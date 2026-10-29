from __future__ import annotations

import json
import logging
import math
import os
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .. import __version__
from ..cli import DB_NAME, default_output_dir, parse_start_url, run_crawl
from ..config import DEFAULT_USER_AGENT, CrawlConfig
from ..crawler import Crawler
from ..export import INTERNAL_COLUMNS, LINK_TYPE_NAMES, export_crawl
from ..store import Store, UrlRecord

logger = logging.getLogger(__name__)

PAGE_LIMIT_MAX = 500
LINKS_LIMIT = 1000
MAX_SEARCH_CHARS = 500
BLOCKED = "Blocked by robots.txt"


@dataclass(frozen=True, slots=True)
class Column:
    key: str
    label: str
    sql: str
    numeric: bool = False


@dataclass(frozen=True, slots=True)
class Report:
    label: str
    source: str  # FROM ... WHERE ..., so filters can be appended with AND
    address: str
    order: str
    columns: tuple[Column, ...]
    filters: tuple[tuple[str, str, str], ...]  # key, label, SQL condition ("" = no filter)


_HTML_OK = "u.status_code BETWEEN 200 AND 299 AND u.content_type LIKE '%html%'"


def _duplicate(field: str) -> str:
    return (
        f"u.{field} != '' AND u.{field} IN (SELECT {field} FROM urls"
        f" WHERE is_internal = 1 AND {field} != '' GROUP BY {field} HAVING COUNT(*) > 1)"
    )


REPORTS: dict[str, Report] = {
    "internal": Report(
        label="Internal",
        source="FROM urls u WHERE u.is_internal = 1",
        address="u.url",
        order="u.id",
        columns=(
            Column("url", "Address", "u.url"),
            Column("status_code", "Status Code", "u.status_code", numeric=True),
            Column("status_text", "Status", "u.status_text"),
            Column("indexability", "Indexability", "u.indexability"),
            Column("indexability_status", "Indexability Status", "u.indexability_status"),
            Column("title", "Title 1", "u.title"),
            Column("title_length", "Title 1 Length", "u.title_length", numeric=True),
            Column("meta_description", "Meta Description 1", "u.meta_description"),
            Column(
                "meta_description_length",
                "Meta Description 1 Length",
                "u.meta_description_length",
                numeric=True,
            ),
            Column("h1_1", "H1-1", "u.h1_1"),
            Column("word_count", "Word Count", "u.word_count", numeric=True),
            Column("depth", "Crawl Depth", "u.depth", numeric=True),
            # Same counting rule as internal_all.csv: hyperlinks only.
            Column(
                "inlinks",
                "Inlinks",
                "(SELECT COUNT(*) FROM links l"
                " WHERE l.target_url = u.url AND l.link_type = 'hyperlink')",
                numeric=True,
            ),
            Column(
                "outlinks",
                "Outlinks",
                "(SELECT COUNT(*) FROM links l"
                " WHERE l.source_id = u.id AND l.link_type = 'hyperlink' AND l.is_internal = 1)",
                numeric=True,
            ),
            Column("response_time_ms", "Response Time (ms)", "u.response_time_ms", numeric=True),
            Column("content_type", "Content Type", "u.content_type"),
        ),
        filters=(
            ("all", "All", ""),
            ("html", "HTML", "u.content_type LIKE '%html%'"),
            ("redirect", "Redirects (3xx)", "u.status_code BETWEEN 300 AND 399"),
            ("client_error", "Client errors (4xx)", "u.status_code BETWEEN 400 AND 499"),
            ("server_error", "Server errors (5xx)", "u.status_code >= 500"),
            ("no_response", "No response", f"u.status_code = 0 AND u.status_text != '{BLOCKED}'"),
            ("blocked", "Blocked by robots.txt", f"u.status_text = '{BLOCKED}'"),
            ("non_indexable", "Non-indexable", "u.indexability = 'Non-Indexable'"),
            ("missing_title", "Missing title", f"{_HTML_OK} AND u.title = ''"),
            ("duplicate_title", "Duplicate title", _duplicate("title")),
            ("long_title", "Title over 60 characters", "u.title_length > 60"),
            (
                "missing_description",
                "Missing meta description",
                f"{_HTML_OK} AND u.meta_description = ''",
            ),
            ("duplicate_description", "Duplicate meta description", _duplicate("meta_description")),
            (
                "long_description",
                "Meta description over 155 characters",
                "u.meta_description_length > 155",
            ),
            ("missing_h1", "Missing H1", f"{_HTML_OK} AND u.h1_count = 0"),
            ("multiple_h1", "More than one H1", "u.h1_count > 1"),
        ),
    ),
    "external": Report(
        label="External",
        # External inlinks count every link type, the same as external_all.csv.
        source=(
            "FROM (SELECT target_url, COUNT(*) AS inlinks FROM links"
            " WHERE is_internal = 0 GROUP BY target_url) e"
            " LEFT JOIN urls u ON u.url = e.target_url WHERE 1 = 1"
        ),
        address="e.target_url",
        order="e.target_url",
        columns=(
            Column("url", "Address", "e.target_url"),
            Column("status_code", "Status Code", "u.status_code", numeric=True),
            Column("status_text", "Status", "u.status_text"),
            Column("content_type", "Content Type", "u.content_type"),
            Column("inlinks", "Inlinks", "e.inlinks", numeric=True),
        ),
        filters=(
            ("all", "All", ""),
            (
                "error",
                "Errors (4xx, 5xx, no response)",
                "u.url IS NOT NULL AND (u.status_code >= 400 OR u.status_code = 0)",
            ),
            ("not_checked", "Not checked", "u.url IS NULL"),
        ),
    ),
}

_FIELD_LABELS = {
    expr.removeprefix("u."): name for name, expr in INTERNAL_COLUMNS if expr.startswith("u.")
} | {"is_internal": "Internal"}
_DETAIL_FIELDS = [f.name for f in fields(UrlRecord) if f.name != "headers"]
_INLINKS_SQL = """
SELECT l.link_type, s.url, l.anchor, l.alt, l.rel, l.follow
FROM links l JOIN urls s ON s.id = l.source_id
WHERE l.target_url = ? ORDER BY l.id LIMIT ?
"""
_OUTLINKS_SQL = """
SELECT l.link_type, l.target_url, l.anchor, l.alt, l.rel, l.follow, t.status_code
FROM links l LEFT JOIN urls t ON t.url = l.target_url
WHERE l.source_id = ? ORDER BY l.id LIMIT ?
"""


def report_info() -> dict[str, Any]:
    return {
        key: {
            "label": report.label,
            "filters": [{"key": k, "label": label} for k, label, _ in report.filters],
        }
        for key, report in REPORTS.items()
    }


def _number(options: Mapping[str, Any], key: str, label: str, default: float, kind: type) -> Any:
    value = options.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        if isinstance(value, bool) or not isinstance(value, int | float | str):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or (kind is int and not number.is_integer()):
            raise ValueError
    except ValueError:
        kind_name = "whole number" if kind is int else "number"
        raise ValueError(f"{label} must be a {kind_name}") from None
    return kind(number)


def _patterns(options: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = options.get(key) or []
    if isinstance(value, str):
        value = value.splitlines()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key} must be a list of patterns")
    return tuple(line.strip() for line in value if line.strip())


def config_from_options(options: Any) -> CrawlConfig:
    if not isinstance(options, Mapping):
        raise ValueError("crawl options must be an object")
    raw = options.get("url")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Enter the address of the site to crawl.")
    start = parse_start_url(raw)
    if start is None:
        raise ValueError(f"{raw.strip()!r} is not an http or https address")
    user_agent = options.get("user_agent") or ""
    if not isinstance(user_agent, str):
        raise ValueError("user agent must be text")
    return CrawlConfig(
        start_url=start,
        concurrency=_number(options, "concurrency", "Concurrency", 5, int),
        max_urls=_number(options, "max_urls", "Max URLs", 0, int),
        max_depth=_number(options, "max_depth", "Max depth", -1, int),
        rate_limit=_number(options, "rate", "Rate", 0.0, float),
        timeout=_number(options, "timeout", "Timeout", 20.0, float),
        user_agent=user_agent.strip() or DEFAULT_USER_AGENT,
        respect_robots=options.get("ignore_robots") is not True,
        follow_nofollow=options.get("follow_nofollow") is True,
        include_subdomains=options.get("include_subdomains") is True,
        check_external=options.get("check_external") is True,
        include=_patterns(options, "include"),
        exclude=_patterns(options, "exclude"),
    )


@contextmanager
def _reader(path: Path) -> Iterator[sqlite3.Connection]:
    # A fresh connection per call: pywebview runs every JS call on its own thread.
    conn = sqlite3.connect(path, timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def _int_arg(value: Any, name: str, default: int, minimum: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be a whole number of at least {minimum}")
    return value


def _like(text: str) -> str:
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _link_rows(rows: list[tuple[Any, ...]]) -> list[list[Any]]:
    out = []
    for row in rows:
        values = list(row)
        values[0] = LINK_TYPE_NAMES.get(values[0], values[0])
        values[5] = "Yes" if values[5] else "No"
        out.append(values)
    return out


def open_in_browser(url: Any) -> dict[str, Any]:
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        return {"ok": False, "error": "Only http and https addresses can be opened."}
    webbrowser.open(url)
    return {"ok": True}


def reveal_folder(folder: Path) -> None:
    if sys.platform == "win32":
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", str(folder)], check=False)
    else:
        subprocess.run(["xdg-open", str(folder)], check=False)


# One crawl at a time on a background thread, plus read-only views of its crawl.db.
class CrawlSession:
    def __init__(
        self, crawls_dir: Path, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.crawls_dir = crawls_dir
        self._transport = transport
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._crawler: Crawler | None = None
        self._mode = "idle"  # idle | crawl | opened
        self._db: Path | None = None
        self._folder: Path | None = None
        self._start_url = ""
        self._result = ""
        self._error = ""
        self._stop_requested = False
        self._elapsed: float | None = None
        self._saved_counts: dict[str, int] = {}

    def _busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def is_busy(self) -> bool:
        with self._lock:
            return self._busy()

    def start(self, options: Any) -> dict[str, Any]:
        try:
            config = config_from_options(options)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            if self._busy():
                return {"ok": False, "error": "A crawl is already running."}
            folder = default_output_dir(config.start_url, self.crawls_dir)
            try:
                folder.mkdir(parents=True)
            except OSError as exc:
                return {"ok": False, "error": f"Could not create {folder}: {exc.strerror or exc}"}
            self._mode, self._folder, self._db, self._crawler = "crawl", folder, None, None
            self._start_url, self._result, self._error = config.start_url, "", ""
            self._stop_requested, self._elapsed, self._saved_counts = False, None, {}
            self._thread = threading.Thread(
                target=self._run, args=(config, folder), name="screamingllamas-crawl"
            )
            self._thread.start()
        return {"ok": True, "folder": str(folder)}

    def _attach(self, crawler: Crawler) -> None:
        with self._lock:
            self._crawler = crawler
            self._db = crawler.store.path
            stop = self._stop_requested
        if stop:
            crawler.stop()

    def _run(self, config: CrawlConfig, folder: Path) -> None:
        db_path = folder / DB_NAME
        result, error = "failed", ""
        try:
            _, result = run_crawl(
                config, db_path, transport=self._transport, on_crawler=self._attach
            )
            with Store.open(db_path) as store:
                export_crawl(store, folder)
        except Exception as exc:  # show the failure in the window instead of losing it
            logger.exception("Crawl of %s failed", config.start_url)
            error = str(exc) or type(exc).__name__
            if result != "failed":
                error = f"crawl saved, but the CSV export failed: {error}"
        with self._lock:
            self._mode, self._result, self._error = "crawl", result, error
            if self._crawler is not None:
                self._elapsed = self._crawler.stats.elapsed

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._busy():
                return {"ok": False, "error": "No crawl is running."}
            self._stop_requested = True
            crawler = self._crawler
        if crawler is not None:
            crawler.stop()
        return {"ok": True}

    def wait(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return not self.is_busy()

    def shutdown(self, timeout: float = 60.0) -> None:
        self.stop()
        self.wait(timeout)

    def open_db(self, path: Path) -> dict[str, Any]:
        # Absolute, so the folder later handed to `open`/Explorer can't look like an option.
        path = Path(path).resolve()
        if self.is_busy():
            return {"ok": False, "error": "Stop the running crawl before opening another one."}
        try:
            with Store.open(path) as store:
                meta = store.get_meta()
                total, blocked, errors = store.query(
                    "SELECT COUNT(*), SUM(status_text = ?),"
                    " SUM(status_code = 0 AND status_text != ?) FROM urls",
                    (BLOCKED, BLOCKED),
                ).fetchone()
        except (OSError, ValueError, sqlite3.Error) as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            if self._busy():
                return {"ok": False, "error": "Stop the running crawl before opening another one."}
            self._mode, self._db, self._folder, self._crawler = "opened", path, path.parent, None
            self._start_url = meta.get("start_url", "")
            self._result, self._error = meta.get("status", ""), ""
            self._stop_requested, self._elapsed = False, None
            self._saved_counts = {
                "crawled": total - (blocked or 0),
                "blocked": blocked or 0,
                "errors": errors or 0,
            }
        return {"ok": True}

    def status(self) -> dict[str, Any]:
        with self._lock:
            busy = self._busy()
            if busy:
                state = "stopping" if self._stop_requested else "running"
            else:
                state = {"crawl": "done"}.get(self._mode, self._mode)
            info: dict[str, Any] = {
                "state": state,
                "result": self._result,
                "error": self._error,
                "start_url": self._start_url,
                "folder": str(self._folder) if self._folder else "",
                "version": __version__,
                **self._saved_counts,
            }
            crawler, elapsed = self._crawler, self._elapsed
        if crawler is not None:
            stats = crawler.stats
            info.update(
                crawled=stats.crawled,
                blocked=stats.blocked,
                errors=stats.errors,
                waiting=crawler.waiting if busy else 0,
                elapsed=round(stats.elapsed if elapsed is None else elapsed, 1),
            )
        return info

    def _current_db(self) -> Path | None:
        with self._lock:
            db = self._db
        return db if db is not None and db.is_file() else None

    def rows(self, tab: Any, query: Any) -> dict[str, Any]:
        report = REPORTS.get(tab) if isinstance(tab, str) else None
        if report is None:
            raise ValueError(f"unknown report {tab!r}")
        if not isinstance(query, Mapping):
            raise ValueError("query must be an object")
        offset = _int_arg(query.get("offset"), "offset", 0, 0)
        limit = min(_int_arg(query.get("limit"), "limit", 200, 1), PAGE_LIMIT_MAX)
        columns = {column.key: column for column in report.columns}
        sort = query.get("sort") or None
        if sort is not None and sort not in columns:
            raise ValueError(f"unknown sort column {sort!r}")
        filters = {key: sql for key, _, sql in report.filters}
        filter_key = query.get("filter") or "all"
        if filter_key not in filters:
            raise ValueError(f"unknown filter {filter_key!r}")
        search = query.get("search") or ""
        if not isinstance(search, str):
            raise ValueError("search must be text")

        # Only whitelisted SQL fragments are interpolated; user values are bound parameters.
        where, params = "", []
        if filters[filter_key]:
            where += f" AND ({filters[filter_key]})"
        if search.strip():
            where += f" AND {report.address} LIKE ? ESCAPE '\\'"
            params.append(_like(search.strip()[:MAX_SEARCH_CHARS]))
        order = report.order
        if sort is not None:
            direction = "DESC" if query.get("desc") is True else "ASC"
            order = f"{columns[sort].sql} {direction}, {report.order}"
        select = ", ".join(column.sql for column in report.columns)

        result: dict[str, Any] = {
            "columns": [
                {"key": c.key, "label": c.label, "numeric": c.numeric} for c in report.columns
            ],
            "rows": [],
            "total": 0,
            "offset": offset,
            "has_crawl": False,
        }
        db = self._current_db()
        if db is None:
            return result
        with _reader(db) as conn:
            result["total"] = conn.execute(
                f"SELECT COUNT(*) {report.source}{where}", params
            ).fetchone()[0]
            result["rows"] = [
                list(row)
                for row in conn.execute(
                    f"SELECT {select} {report.source}{where} ORDER BY {order} LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                )
            ]
        result["has_crawl"] = True
        return result

    def url_info(self, url: Any) -> dict[str, Any]:
        if not isinstance(url, str):
            raise ValueError("url must be text")
        info: dict[str, Any] = {
            "url": url,
            "crawled": False,
            "details": [],
            "headers": [],
            "inlinks": {"columns": ["Type", "From", "Anchor Text", "Alt Text", "Rel", "Follow"]},
            "outlinks": {
                "columns": ["Type", "To", "Anchor Text", "Alt Text", "Rel", "Follow", "Status Code"]
            },
        }
        info["inlinks"].update(rows=[], total=0)
        info["outlinks"].update(rows=[], total=0)
        db = self._current_db()
        if db is None:
            return info
        with _reader(db) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM urls WHERE url = ?", (url,)).fetchone()
            inlinks = conn.execute(_INLINKS_SQL, (url, LINKS_LIMIT)).fetchall()
            info["inlinks"]["total"] = conn.execute(
                "SELECT COUNT(*) FROM links WHERE target_url = ?", (url,)
            ).fetchone()[0]
            info["inlinks"]["rows"] = _link_rows([tuple(r) for r in inlinks])
            if row is None:
                return info
            info["crawled"] = True
            for name in _DETAIL_FIELDS:
                value = row[name]
                if name == "is_internal":
                    value = "Yes" if value else "No"
                label = _FIELD_LABELS.get(name, name.replace("_", " ").capitalize())
                info["details"].append([label, value])
            try:
                headers = json.loads(row["headers"]) if row["headers"] else {}
            except json.JSONDecodeError:
                headers = {}
            if isinstance(headers, dict):
                info["headers"] = [[str(k), str(v)] for k, v in headers.items()]
            outlinks = conn.execute(_OUTLINKS_SQL, (row["id"], LINKS_LIMIT)).fetchall()
            info["outlinks"]["total"] = conn.execute(
                "SELECT COUNT(*) FROM links WHERE source_id = ?", (row["id"],)
            ).fetchone()[0]
            info["outlinks"]["rows"] = _link_rows([tuple(r) for r in outlinks])
        return info

    def show_folder(self) -> dict[str, Any]:
        with self._lock:
            folder = self._folder
        if folder is None or not folder.is_dir():
            return {"ok": False, "error": "There is no crawl folder to show yet."}
        reveal_folder(folder)
        return {"ok": True}


def make_api(session: CrawlSession, choose_database: Callable[[], Path | None]) -> object:
    # pywebview resolves dotted names from JS with getattr, so state lives in this
    # closure rather than on attributes a page script could reach.
    class Api:
        def reports(self) -> dict[str, Any]:
            return report_info()

        def start(self, options: Any) -> dict[str, Any]:
            return session.start(options)

        def stop(self) -> dict[str, Any]:
            return session.stop()

        def status(self) -> dict[str, Any]:
            return session.status()

        def rows(self, tab: Any, query: Any) -> dict[str, Any]:
            return session.rows(tab, query)

        def url_info(self, url: Any) -> dict[str, Any]:
            return session.url_info(url)

        def open_crawl(self) -> dict[str, Any]:
            path = choose_database()
            if path is None:
                return {"ok": False, "cancelled": True}
            return session.open_db(path)

        def show_folder(self) -> dict[str, Any]:
            return session.show_folder()

        def open_in_browser(self, url: Any) -> dict[str, Any]:
            return open_in_browser(url)

    return Api()
