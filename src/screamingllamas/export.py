from __future__ import annotations

import csv
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

from .store import Store

INTERNAL_FILE = "internal_all.csv"
EXTERNAL_FILE = "external_all.csv"
INLINKS_FILE = "all_inlinks.csv"
EXPORT_FILES = (INTERNAL_FILE, EXTERNAL_FILE, INLINKS_FILE)

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
LINK_TYPE_NAMES = {
    "hyperlink": "Hyperlink",
    "image": "Image",
    "canonical": "Canonical",
    "hreflang": "Hreflang",
    "next": "Rel Next",
    "prev": "Rel Prev",
    "redirect": "HTTP Redirect",
    "meta_refresh": "Meta Refresh",
}

INTERNAL_COLUMNS: list[tuple[str, str]] = [
    ("Address", "u.url"),
    ("Content Type", "u.content_type"),
    ("Status Code", "u.status_code"),
    ("Status", "u.status_text"),
    ("Indexability", "u.indexability"),
    ("Indexability Status", "u.indexability_status"),
    ("Title 1", "u.title"),
    ("Title 1 Length", "u.title_length"),
    ("Title Count", "u.title_count"),
    ("Meta Description 1", "u.meta_description"),
    ("Meta Description 1 Length", "u.meta_description_length"),
    ("Meta Description Count", "u.meta_description_count"),
    ("H1-1", "u.h1_1"),
    ("H1-2", "u.h1_2"),
    ("H1 Count", "u.h1_count"),
    ("H2-1", "u.h2_1"),
    ("H2-2", "u.h2_2"),
    ("H2 Count", "u.h2_count"),
    ("Meta Robots 1", "u.meta_robots"),
    ("X-Robots-Tag 1", "u.x_robots_tag"),
    ("Canonical Link Element 1", "u.canonical"),
    ("Size (bytes)", "u.size_bytes"),
    ("Word Count", "u.word_count"),
    ("Language", "u.lang"),
    ("Crawl Depth", "u.depth"),
    ("Inlinks", "COALESCE(i.inlinks, 0)"),
    ("Unique Inlinks", "COALESCE(i.unique_inlinks, 0)"),
    ("Outlinks", "COALESCE(o.outlinks, 0)"),
    ("External Outlinks", "COALESCE(o.external_outlinks, 0)"),
    ("Response Time (ms)", "u.response_time_ms"),
    ("Redirect URL", "u.redirect_url"),
    ("Redirect Type", "u.redirect_type"),
    ("Hash", "u.content_hash"),
    ("Crawl Timestamp", "u.crawled_at"),
    ("Error", "u.error"),
]

_INTERNAL_SQL = f"""
WITH i AS (
  SELECT target_url, COUNT(*) AS inlinks, COUNT(DISTINCT source_id) AS unique_inlinks
  FROM links WHERE link_type = 'hyperlink' GROUP BY target_url
), o AS (
  SELECT source_id, SUM(is_internal) AS outlinks, SUM(1 - is_internal) AS external_outlinks
  FROM links WHERE link_type = 'hyperlink' GROUP BY source_id
)
SELECT {", ".join(expr for _, expr in INTERNAL_COLUMNS)}
FROM urls u
LEFT JOIN i ON i.target_url = u.url
LEFT JOIN o ON o.source_id = u.id
WHERE u.is_internal = 1
ORDER BY u.id
"""

_EXTERNAL_HEADER = [
    "Address",
    "Status Code",
    "Status",
    "Content Type",
    "Size (bytes)",
    "Response Time (ms)",
    "Inlinks",
]
_EXTERNAL_SQL = """
WITH e AS (
  SELECT target_url, COUNT(*) AS inlinks FROM links WHERE is_internal = 0 GROUP BY target_url
)
SELECT e.target_url, u.status_code, u.status_text, u.content_type, u.size_bytes,
       u.response_time_ms, e.inlinks
FROM e LEFT JOIN urls u ON u.url = e.target_url
ORDER BY e.target_url
"""

_INLINKS_HEADER = [
    "Type",
    "Source",
    "Destination",
    "Anchor",
    "Alt Text",
    "Rel",
    "Hreflang",
    "Follow",
    "Status Code",
    "Status",
]
_INLINKS_SQL = """
SELECT l.link_type, s.url, l.target_url, l.anchor, l.alt, l.rel, l.hreflang, l.follow,
       t.status_code, t.status_text
FROM links l
JOIN urls s ON s.id = l.source_id
LEFT JOIN urls t ON t.url = l.target_url
ORDER BY l.id
"""


def _cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        # Crawled text is untrusted; stop Excel/Sheets from running it as a formula.
        return "'" + value
    return value


def _write_csv(path: Path, header: Sequence[str], rows: Iterable[Sequence[object]]) -> int:
    count = 0
    # utf-8-sig so Excel on Windows detects UTF-8 instead of guessing a code page.
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for row in rows:
            writer.writerow([_cell(value) for value in row])
            count += 1
    return count


def _inlink_rows(store: Store) -> Iterable[list[object]]:
    for row in store.query(_INLINKS_SQL):
        values: list[object] = list(row)
        values[0] = LINK_TYPE_NAMES.get(row[0], row[0])
        values[7] = "true" if row[7] else "false"
        yield values


def export_crawl(store: Store, out_dir: Path) -> list[tuple[Path, int]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[str, Sequence[str], Callable[[], Iterable[Sequence[object]]]]] = [
        (
            INTERNAL_FILE,
            [name for name, _ in INTERNAL_COLUMNS],
            lambda: store.query(_INTERNAL_SQL),
        ),
        (EXTERNAL_FILE, _EXTERNAL_HEADER, lambda: store.query(_EXTERNAL_SQL)),
        (INLINKS_FILE, _INLINKS_HEADER, lambda: _inlink_rows(store)),
    ]
    written: list[tuple[Path, int]] = []
    for name, header, rows in jobs:
        path = out_dir / name
        written.append((path, _write_csv(path, header, rows())))
    return written
