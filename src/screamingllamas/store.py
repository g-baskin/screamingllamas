from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, fields
from pathlib import Path
from types import TracebackType
from typing import Any

from .extract import Link

SCHEMA_VERSION = "1"
_COMMIT_EVERY = 50


@dataclass(slots=True)
class UrlRecord:
    url: str
    is_internal: bool
    depth: int
    status_code: int = 0
    status_text: str = ""
    content_type: str = ""
    size_bytes: int = 0
    response_time_ms: int = 0
    redirect_url: str = ""
    redirect_type: str = ""
    indexability: str = ""
    indexability_status: str = ""
    title: str = ""
    title_length: int = 0
    title_count: int = 0
    meta_description: str = ""
    meta_description_length: int = 0
    meta_description_count: int = 0
    meta_robots: str = ""
    x_robots_tag: str = ""
    canonical: str = ""
    h1_1: str = ""
    h1_2: str = ""
    h1_count: int = 0
    h2_1: str = ""
    h2_2: str = ""
    h2_count: int = 0
    lang: str = ""
    word_count: int = 0
    content_hash: str = ""
    crawled_at: str = ""
    headers: str = ""
    error: str = ""


URL_COLUMNS = tuple(f.name for f in fields(UrlRecord))
_SQL_TYPES = {"str": "TEXT", "int": "INTEGER", "bool": "INTEGER"}
_URL_COLUMN_DEFS = ",\n  ".join(
    f"{f.name} {_SQL_TYPES[str(f.type)]} NOT NULL" + (" UNIQUE" if f.name == "url" else "")
    for f in fields(UrlRecord)
)
_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS urls (
  id INTEGER PRIMARY KEY,
  {_URL_COLUMN_DEFS}
);
CREATE TABLE IF NOT EXISTS links (
  id INTEGER PRIMARY KEY,
  source_id INTEGER NOT NULL REFERENCES urls(id),
  target_url TEXT NOT NULL,
  link_type TEXT NOT NULL,
  anchor TEXT NOT NULL,
  alt TEXT,
  rel TEXT NOT NULL,
  hreflang TEXT NOT NULL,
  is_internal INTEGER NOT NULL,
  follow INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS links_target ON links(target_url);
CREATE INDEX IF NOT EXISTS links_source ON links(source_id);
"""
_PLACEHOLDERS = ", ".join("?" for _ in URL_COLUMNS)
_INSERT_URL = f"INSERT INTO urls ({', '.join(URL_COLUMNS)}) VALUES ({_PLACEHOLDERS})"
_INSERT_LINK = (
    "INSERT INTO links (source_id, target_url, link_type, anchor, alt, rel, hreflang,"
    " is_internal, follow) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


class Store:
    def __init__(self, path: Path, conn: sqlite3.Connection) -> None:
        self.path = path
        self._conn = conn
        self._conn.row_factory = sqlite3.Row
        self._pending = 0

    @classmethod
    def create(cls, path: Path) -> Store:
        if path.exists():
            raise FileExistsError(f"{path} already exists")
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(_SCHEMA)
        store = cls(path, conn)
        store.set_meta(schema_version=SCHEMA_VERSION)
        return store

    @classmethod
    def open(cls, path: Path) -> Store:
        if not path.is_file():
            raise FileNotFoundError(f"{path} does not exist")
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.DatabaseError:
            row = None
        if row is None or row[0] != SCHEMA_VERSION:
            conn.close()
            raise ValueError(f"{path} is not a Screaming Llamas crawl database")
        return cls(path, conn)

    def __enter__(self) -> Store:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def set_meta(self, **values: str) -> None:
        self._conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            values.items(),
        )
        self._conn.commit()

    def get_meta(self) -> dict[str, str]:
        return {
            row["key"]: row["value"] for row in self._conn.execute("SELECT key, value FROM meta")
        }

    def add_url(self, record: UrlRecord, *, ignore_existing: bool = False) -> int:
        sql = (
            _INSERT_URL.replace("INSERT", "INSERT OR IGNORE", 1) if ignore_existing else _INSERT_URL
        )
        cursor = self._conn.execute(sql, tuple(getattr(record, c) for c in URL_COLUMNS))
        self._tick()
        return cursor.lastrowid or 0

    def add_links(self, source_id: int, links: Iterable[tuple[Link, bool, bool]]) -> None:
        self._conn.executemany(
            _INSERT_LINK,
            (
                (
                    source_id,
                    link.url,
                    link.link_type,
                    link.anchor,
                    link.alt,
                    link.rel,
                    link.hreflang,
                    internal,
                    follow,
                )
                for link, internal, follow in links
            ),
        )

    def get_url(self, url: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM urls WHERE url = ?", (url,)).fetchone()
        return dict(row) if row else None

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> Iterator[sqlite3.Row]:
        return self._conn.execute(sql, params)

    def _tick(self) -> None:
        self._pending += 1
        if self._pending >= _COMMIT_EVERY:
            self.commit()

    def commit(self) -> None:
        self._conn.commit()
        self._pending = 0

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()
