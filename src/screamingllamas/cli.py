from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import httpx

from . import __version__
from .config import DEFAULT_USER_AGENT, CrawlConfig
from .crawler import Crawler, CrawlStats, now_iso
from .export import EXPORT_FILES, export_crawl
from .store import Store
from .urls import host_of, normalize_url

DB_NAME = "crawl.db"
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="screamingllamas", description="Crawl a website and export SEO data to CSV."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    crawl = commands.add_parser("crawl", help="crawl a site, save crawl.db and export CSVs")
    crawl.add_argument(
        "url", help="start URL, e.g. https://example.com (https:// is added if missing)"
    )
    crawl.add_argument(
        "-o",
        "--output",
        type=Path,
        help="folder for crawl.db and the CSVs (default: ./crawls/<host>-<date>-<time>)",
    )
    crawl.add_argument(
        "--max-urls", type=int, default=0, help="stop after this many URLs (0 = no limit)"
    )
    crawl.add_argument(
        "--max-depth", type=int, default=-1, help="max clicks from the start URL (-1 = no limit)"
    )
    crawl.add_argument(
        "--concurrency", type=int, default=5, help="parallel requests (default 5, max 100)"
    )
    crawl.add_argument(
        "--rate", type=float, default=0.0, help="max requests per second (0 = no limit)"
    )
    crawl.add_argument(
        "--timeout", type=float, default=20.0, help="seconds to wait for each response"
    )
    crawl.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header to send")
    crawl.add_argument(
        "--ignore-robots", action="store_true", help="crawl URLs that robots.txt blocks"
    )
    crawl.add_argument("--follow-nofollow", action="store_true", help="follow nofollow links too")
    crawl.add_argument(
        "--include-subdomains",
        action="store_true",
        help="treat subdomains of the start host as internal",
    )
    crawl.add_argument(
        "--check-external",
        action="store_true",
        help="request external links to record their status",
    )
    crawl.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="REGEX",
        help="only crawl internal URLs that match (can repeat)",
    )
    crawl.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="REGEX",
        help="skip URLs that match (can repeat)",
    )
    crawl.add_argument("-q", "--quiet", action="store_true", help="hide progress lines")

    export = commands.add_parser("export", help="write the CSVs again from a crawl.db")
    export.add_argument("database", type=Path, help="path to a crawl.db")
    export.add_argument(
        "-o", "--output", type=Path, help="folder for the CSVs (default: next to the database)"
    )
    export.add_argument(
        "--force", action="store_true", help="overwrite CSV files that already exist"
    )

    ui = commands.add_parser("ui", help="open the desktop app (needs the 'ui' extra)")
    ui.add_argument("database", nargs="?", type=Path, help="a crawl.db to open (optional)")
    ui.add_argument("--crawls-dir", type=Path, help="folder for new crawls (default: ./crawls)")
    return parser


def _error(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)


def default_output_dir(start_url: str, parent: Path) -> Path:
    # Keep only characters that are safe in folder names on Windows and macOS.
    host = re.sub(r"[^A-Za-z0-9.-]", "_", host_of(start_url)) or "crawl"
    base = parent / f"{host}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    candidate, n = base, 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}-{n}")
        n += 1
    return candidate


def parse_start_url(raw: str) -> str | None:
    raw = raw.strip()
    return normalize_url(raw if "://" in raw else f"https://{raw}")


def run_crawl(
    config: CrawlConfig,
    db_path: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    on_crawler: Callable[[Crawler], None] | None = None,
) -> tuple[CrawlStats, str]:
    with Store.create(db_path) as store:
        store.set_meta(
            tool_version=__version__,
            start_url=config.start_url,
            started_at=now_iso(),
            status="running",
            config=json.dumps(asdict(config)),
        )
        crawler = Crawler(config, store, transport=transport)
        if on_crawler is not None:
            on_crawler(crawler)
        status = "failed"
        try:
            asyncio.run(crawler.run())
            status = "interrupted" if crawler.stopped else "completed"
        except KeyboardInterrupt:
            status = "interrupted"
        finally:
            store.set_meta(status=status, finished_at=now_iso())
        return crawler.stats, status


def _print_written(out_dir: Path, written: list[tuple[Path, int]]) -> None:
    print(f"Saved to {out_dir}")
    for path, rows in written:
        print(f"  {path.name} ({rows} rows)")


def _crawl_command(args: argparse.Namespace) -> int:
    start = parse_start_url(args.url)
    if start is None:
        _error(f"{args.url!r} is not an http or https URL")
        return EXIT_USAGE
    try:
        config = CrawlConfig(
            start_url=start,
            concurrency=args.concurrency,
            max_urls=args.max_urls,
            max_depth=args.max_depth,
            rate_limit=args.rate,
            timeout=args.timeout,
            user_agent=args.user_agent,
            respect_robots=not args.ignore_robots,
            follow_nofollow=args.follow_nofollow,
            include_subdomains=args.include_subdomains,
            check_external=args.check_external,
            include=tuple(args.include),
            exclude=tuple(args.exclude),
        )
    except ValueError as exc:
        _error(str(exc))
        return EXIT_USAGE

    out_dir: Path = args.output or default_output_dir(start, Path.cwd() / "crawls")
    existing = [name for name in (DB_NAME, *EXPORT_FILES) if (out_dir / name).exists()]
    if existing:
        _error(f"{out_dir} already has {', '.join(existing)}; choose another --output folder")
        return EXIT_ERROR
    out_dir.mkdir(parents=True, exist_ok=True)

    logging.getLogger(__name__).info("Crawling %s", start)
    db_path = out_dir / DB_NAME
    stats, status = run_crawl(config, db_path)
    with Store.open(db_path) as store:
        written = export_crawl(store, out_dir)

    word = "Stopped after" if status == "interrupted" else "Crawled"
    print(
        f"{word} {stats.crawled} URLs in {stats.elapsed:.1f}s"
        f" ({stats.blocked} blocked by robots.txt, {stats.errors} without a response)"
    )
    _print_written(out_dir, [(db_path, stats.crawled + stats.blocked), *written])
    return EXIT_INTERRUPTED if status == "interrupted" else 0


def _export_command(args: argparse.Namespace) -> int:
    db_path: Path = args.database
    out_dir: Path = args.output or db_path.parent
    existing = [name for name in EXPORT_FILES if (out_dir / name).exists()]
    if existing and not args.force:
        _error(f"{out_dir} already has {', '.join(existing)}; add --force to overwrite them")
        return EXIT_ERROR
    try:
        with Store.open(db_path) as store:
            written = export_crawl(store, out_dir)
    except (FileNotFoundError, ValueError) as exc:
        _error(str(exc))
        return EXIT_ERROR
    _print_written(out_dir, written)
    return 0


def _ui_command(args: argparse.Namespace) -> int:
    from .ui.session import CrawlSession

    session = CrawlSession((args.crawls_dir or Path.cwd() / "crawls").resolve())
    if args.database is not None:
        opened = session.open_db(args.database)
        if not opened["ok"]:
            _error(opened["error"])
            return EXIT_ERROR
    try:
        from .ui.app import launch
    except ModuleNotFoundError as exc:
        if exc.name != "webview":
            raise
        _error(
            "the desktop app needs the 'ui' extra: run uv sync --extra ui in the project folder,"
            ' or install from there with pipx install ".[ui]"'
        )
        return EXIT_ERROR
    launch(session)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if getattr(args, "quiet", False) else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    # httpx logs every request at INFO, which would bury the progress lines.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "crawl":
        return _crawl_command(args)
    if args.command == "ui":
        return _ui_command(args)
    return _export_command(args)
