from __future__ import annotations

import codecs
import re
import time
from dataclasses import dataclass, field
from typing import Literal

import httpx

HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
TOO_MANY_REDIRECTS = "too_many_redirects"
_CHARSET_PARAM = re.compile(r"charset\s*=\s*[\"']?([\w.:-]+)", re.I)
_META_CHARSET = re.compile(rb"<meta[^>]+charset\s*=\s*[\"']?\s*([\w.:-]+)", re.I)
# The HTML standard decodes these legacy labels as windows-1252.
_WINDOWS_1252_LABELS = frozenset(
    {"iso-8859-1", "iso8859-1", "latin1", "latin-1", "l1", "ascii", "us-ascii", "cp819", "ibm819"}
)
_BOMS = (
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)

BodyMode = Literal["always", "html", "never"]


@dataclass(slots=True)
class FetchResult:
    url: str
    status_code: int = 0
    status_text: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    size_bytes: int = 0
    truncated: bool = False
    elapsed_ms: int = 0
    error: str = ""


def mime_type(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def looks_like_html(body: bytes) -> bool:
    return body[:1024].lstrip().lower().startswith((b"<!doctype html", b"<html"))


def _flatten(headers: httpx.Headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.multi_items():
        key = key.lower()
        out[key] = f"{out[key]}, {value}" if key in out else value
    return out


def _content_length(headers: httpx.Headers) -> int:
    value = headers.get("content-length", "")
    return int(value) if value.isdigit() else 0


async def fetch(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    body: BodyMode = "html",
    follow_redirects: bool = False,
) -> FetchResult:
    result = FetchResult(url=url)
    started = time.perf_counter()
    try:
        async with client.stream("GET", url, follow_redirects=follow_redirects) as response:
            result.status_code = response.status_code
            result.status_text = response.reason_phrase
            result.headers = _flatten(response.headers)
            mime = mime_type(response.headers.get("content-type"))
            if body == "always" or (body == "html" and (not mime or mime in HTML_TYPES)):
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    if total + len(chunk) > max_bytes:
                        chunks.append(chunk[: max_bytes - total])
                        total = max_bytes
                        result.truncated = True
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                result.body = b"".join(chunks)
                result.size_bytes = total
            else:
                result.size_bytes = _content_length(response.headers)
    except httpx.TimeoutException as exc:
        _fail(result, "Timeout", "timeout", exc)
    except httpx.TooManyRedirects as exc:
        _fail(result, "Too Many Redirects", TOO_MANY_REDIRECTS, exc)
    except (httpx.InvalidURL, httpx.UnsupportedProtocol) as exc:
        _fail(result, "Invalid URL", "invalid_url", exc)
    except httpx.TransportError as exc:
        _fail(result, "Connection Error", "connection", exc)
    except httpx.HTTPError as exc:
        _fail(result, "HTTP Error", "http", exc)
    result.elapsed_ms = round((time.perf_counter() - started) * 1000)
    return result


def _fail(result: FetchResult, status_text: str, kind: str, exc: Exception) -> None:
    result.status_code = 0
    result.status_text = status_text
    result.body = b""
    result.error = f"{kind}: {exc}" if str(exc) else kind


def _codec_for(label: str | None) -> str:
    if not label:
        return "utf-8"
    label = label.strip().lower()
    if label in _WINDOWS_1252_LABELS:
        return "cp1252"
    try:
        name = codecs.lookup(label).name
    except LookupError:
        return "utf-8"
    # A utf-16 label without a BOM is almost always wrong; browsers use utf-8.
    return "utf-8" if name.startswith("utf-16") else name


def decode_html(body: bytes, content_type: str | None) -> str:
    for bom, encoding in _BOMS:
        if body.startswith(bom):
            return body[len(bom) :].decode(encoding, "replace")
    match = _CHARSET_PARAM.search(content_type or "")
    label = match.group(1) if match else None
    if label is None:
        meta = _META_CHARSET.search(body[:1024])
        label = meta.group(1).decode("ascii", "ignore") if meta else None
    try:
        return body.decode(_codec_for(label), "replace")
    except (LookupError, ValueError):
        # Labels like "base64" or "idna" name codecs that can't decode a web page.
        return body.decode("utf-8", "replace")
