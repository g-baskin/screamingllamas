from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
PATH_SAFE = "/:@!$&'()*+,;=-._~%"
_QUERY_SAFE = PATH_SAFE + "?"
_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")
_C0_AND_SPACE = "".join(chr(i) for i in range(0x21))
_REMOVE_INSIDE = str.maketrans("", "", "\t\n\r")


def encode_component(value: str, safe: str) -> str:
    return _ESCAPE.sub(lambda m: m.group(0).upper(), quote(value, safe=safe))


def _remove_dot_segments(path: str) -> str:
    if "/." not in path:
        return path
    segments = path.split("/")
    out: list[str] = []
    for segment in segments:
        if segment == "..":
            if len(out) > 1:
                out.pop()
        elif segment != ".":
            out.append(segment)
    if segments[-1] in (".", ".."):
        out.append("")
    return "/".join(out)


def normalize_url(url: str, base: str | None = None) -> str | None:
    # Browsers trim C0/space from the ends and drop tabs/newlines anywhere in an href.
    url = url.strip(_C0_AND_SPACE).translate(_REMOVE_INSIDE)
    if base is not None:
        url = urljoin(base, url)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    host = parts.hostname
    if not host:
        return None
    if not host.isascii():
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            return None
    if ":" in host:
        host = f"[{host}]"
    netloc = host if port in (None, _DEFAULT_PORTS[scheme]) else f"{host}:{port}"
    path = _remove_dot_segments(encode_component(parts.path, PATH_SAFE)) or "/"
    query = encode_component(parts.query, _QUERY_SAFE)
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    return urlsplit(url).hostname or ""


class Scope:
    def __init__(
        self,
        start_url: str,
        *,
        include_subdomains: bool = False,
        include: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> None:
        self.start_url = start_url
        self.hosts: set[str] = set()
        self._domains: set[str] = set()
        self._include_subdomains = include_subdomains
        self._include = [re.compile(p) for p in include]
        self._exclude = [re.compile(p) for p in exclude]
        self.add_host(host_of(start_url))

    def add_host(self, host: str) -> None:
        self.hosts.add(host)
        self._domains.add(host.removeprefix("www."))

    def is_internal(self, url: str) -> bool:
        host = host_of(url)
        if host in self.hosts:
            return True
        if not self._include_subdomains:
            return False
        return any(host == d or host.endswith("." + d) for d in self._domains)

    def should_crawl(self, url: str) -> bool:
        if url == self.start_url:
            return True
        if not self.is_internal(url):
            return False
        if self._include and not any(p.search(url) for p in self._include):
            return False
        return not any(p.search(url) for p in self._exclude)
