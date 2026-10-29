from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx

from .config import CrawlConfig
from .extract import Link, PageData, extract_page
from .fetch import (
    HTML_TYPES,
    TOO_MANY_REDIRECTS,
    decode_html,
    fetch,
    looks_like_html,
    mime_type,
)
from .robots import RobotsRules, agent_token
from .store import Store, UrlRecord
from .urls import Scope, host_of, normalize_url

logger = logging.getLogger(__name__)

ROBOTS_MAX_BYTES = 500 * 1024
PROGRESS_SECONDS = 2.0
_FOLLOWED_TYPES = frozenset({"hyperlink", "canonical", "redirect", "meta_refresh"})
_NOINDEX = re.compile(r"\b(noindex|none)\b", re.I)
_NOFOLLOW = re.compile(r"\b(nofollow|none)\b", re.I)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(slots=True)
class CrawlStats:
    crawled: int = 0
    blocked: int = 0
    errors: int = 0
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


@dataclass(frozen=True, slots=True)
class _Item:
    url: str
    depth: int


class RateLimiter:
    def __init__(self, per_second: float) -> None:
        self._interval = 1 / per_second if per_second > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next = 0.0

    async def wait(self) -> None:
        if not self._interval:
            return
        async with self._lock:
            delay = self._next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = max(time.monotonic(), self._next) + self._interval


def indexability(record: UrlRecord) -> tuple[str, str]:
    code = record.status_code
    if code == 0:
        return "Non-Indexable", "No Response"
    if 300 <= code < 400 or record.redirect_type == "Meta Refresh":
        return "Non-Indexable", "Redirected"
    if 400 <= code < 500:
        return "Non-Indexable", "Client Error"
    if code >= 500:
        return "Non-Indexable", "Server Error"
    if _NOINDEX.search(record.meta_robots) or _NOINDEX.search(record.x_robots_tag):
        return "Non-Indexable", "noindex"
    if record.canonical and record.canonical != record.url:
        return "Non-Indexable", "Canonicalised"
    return "Indexable", ""


def _apply_page(record: UrlRecord, page: PageData) -> None:
    record.title_count = len(page.titles)
    record.title = page.titles[0] if page.titles else ""
    record.title_length = len(record.title)
    record.meta_description_count = len(page.meta_descriptions)
    record.meta_description = page.meta_descriptions[0] if page.meta_descriptions else ""
    record.meta_description_length = len(record.meta_description)
    record.meta_robots = ", ".join(page.meta_robots)
    record.canonical = page.canonicals[0] if page.canonicals else ""
    record.h1_1, record.h1_2 = (page.h1 + ["", ""])[:2]
    record.h1_count = len(page.h1)
    record.h2_1, record.h2_2 = (page.h2 + ["", ""])[:2]
    record.h2_count = len(page.h2)
    record.lang = page.lang
    record.word_count = page.word_count
    if page.meta_refresh and not record.redirect_url:
        record.redirect_url = page.meta_refresh
        record.redirect_type = "Meta Refresh"


class Crawler:
    def __init__(
        self,
        config: CrawlConfig,
        store: Store,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        start = normalize_url(config.start_url)
        if start is None:
            raise ValueError(f"not a crawlable http(s) URL: {config.start_url}")
        self.config = config
        self.start_url = start
        self.store = store
        self.stats = CrawlStats()
        self.scope = Scope(
            start,
            include_subdomains=config.include_subdomains,
            include=config.include,
            exclude=config.exclude,
        )
        self._transport = transport
        self._agent = agent_token(config.user_agent)
        self._limiter = RateLimiter(config.rate_limit)
        self._seen: set[str] = set()
        self._queue: asyncio.Queue[_Item] = asyncio.Queue()
        self._robots: dict[str, asyncio.Task[RobotsRules]] = {}
        self._client: httpx.AsyncClient | None = None
        self._stop_event: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.stopped = False

    @property
    def waiting(self) -> int:
        return self._queue.qsize()

    def stop(self) -> None:
        # Safe from any thread: run() then finishes cleanly with what it has so far.
        self.stopped = True
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            with contextlib.suppress(RuntimeError):  # the loop already closed
                loop.call_soon_threadsafe(event.set)

    async def run(self) -> CrawlStats:
        self.stats = CrawlStats()
        # Set the event before the loop so stop() never sees a loop without an event.
        self._stop_event = asyncio.Event()
        self._loop = asyncio.get_running_loop()
        if self.stopped:
            return self.stats
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        }
        limits = httpx.Limits(
            max_connections=self.config.concurrency * 2,
            max_keepalive_connections=self.config.concurrency,
        )
        async with httpx.AsyncClient(
            headers=headers,
            timeout=self.config.timeout,
            follow_redirects=False,
            max_redirects=5,
            limits=limits,
            transport=self._transport,
        ) as client:
            self._client = client
            self._enqueue(self.start_url, 0)
            tasks = [asyncio.create_task(self._worker()) for _ in range(self.config.concurrency)]
            tasks.append(asyncio.create_task(self._report_progress()))
            finish = [
                asyncio.create_task(self._queue.join()),
                asyncio.create_task(self._stop_event.wait()),
            ]
            try:
                await asyncio.wait(finish, return_when=asyncio.FIRST_COMPLETED)
            finally:
                pending = [*tasks, *finish, *self._robots.values()]
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                self.store.commit()
        return self.stats

    def _enqueue(self, url: str, depth: int) -> None:
        if url in self._seen:
            return
        if self.config.max_urls and len(self._seen) >= self.config.max_urls:
            return
        self._seen.add(url)
        self._queue.put_nowait(_Item(url, depth))

    async def _report_progress(self) -> None:
        while True:
            await asyncio.sleep(PROGRESS_SECONDS)
            rate = self.stats.crawled / max(self.stats.elapsed, 0.001)
            logger.info(
                "Crawled %d URLs, %d waiting (%.1f URL/s)",
                self.stats.crawled,
                self._queue.qsize(),
                rate,
            )

    async def _worker(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                await self._process(item)
            except Exception as exc:  # one bad page must not stop the whole crawl
                logger.exception("Unexpected error while crawling %s", item.url)
                self.stats.errors += 1
                record = UrlRecord(
                    url=item.url,
                    is_internal=self.scope.is_internal(item.url),
                    depth=item.depth,
                    status_text="Crawler Error",
                    crawled_at=now_iso(),
                    error=repr(exc),
                )
                self.store.add_url(record, ignore_existing=True)
            finally:
                self._queue.task_done()

    async def _process(self, item: _Item) -> None:
        assert self._client is not None
        internal = self.scope.is_internal(item.url)
        record = UrlRecord(
            url=item.url, is_internal=internal, depth=item.depth, crawled_at=now_iso()
        )

        if self.config.respect_robots:
            rules = await self._robots_for(item.url)
            if not rules.is_allowed(item.url, self._agent):
                record.status_text = record.indexability_status = "Blocked by robots.txt"
                record.indexability = "Non-Indexable"
                self.store.add_url(record)
                self.stats.blocked += 1
                return

        await self._limiter.wait()
        result = await fetch(
            self._client,
            item.url,
            max_bytes=self.config.max_page_bytes,
            body="html" if internal else "never",
        )
        self.stats.crawled += 1
        if result.error:
            self.stats.errors += 1
        record.status_code = result.status_code
        record.status_text = result.status_text
        record.content_type = result.headers.get("content-type", "")
        record.size_bytes = result.size_bytes
        record.response_time_ms = result.elapsed_ms
        record.x_robots_tag = result.headers.get("x-robots-tag", "")
        record.headers = json.dumps(result.headers)
        record.error = result.error
        if result.truncated:
            record.error = f"page cut off at {self.config.max_page_bytes} bytes"

        links: list[Link] = []
        location = result.headers.get("location")
        if 300 <= result.status_code < 400 and location:
            target = normalize_url(location, item.url)
            if target:
                record.redirect_url = target
                record.redirect_type = "HTTP Redirect"
                links.append(Link(target, "redirect"))
                if item.url == self.start_url and self._is_www_variant(target):
                    logger.info("Start URL redirects to %s; crawling that host too", target)
                    self.scope.add_host(host_of(target))

        mime = mime_type(record.content_type)
        is_html = mime in HTML_TYPES or (not mime and looks_like_html(result.body))
        if internal and 200 <= result.status_code < 300 and result.body and is_html:
            page = extract_page(decode_html(result.body, record.content_type), item.url)
            _apply_page(record, page)
            links.extend(page.links)
            record.content_hash = hashlib.md5(result.body, usedforsecurity=False).hexdigest()

        record.indexability, record.indexability_status = indexability(record)
        source_id = self.store.add_url(record)

        page_nofollow = bool(
            _NOFOLLOW.search(record.meta_robots) or _NOFOLLOW.search(record.x_robots_tag)
        )
        rows: list[tuple[Link, bool, bool]] = []
        for link in links:
            follow = not link.nofollow and not (page_nofollow and link.link_type == "hyperlink")
            rows.append((link, self.scope.is_internal(link.url), follow))
            if internal:
                self._maybe_enqueue(link, follow, item.depth + 1)
        self.store.add_links(source_id, rows)

    def _is_www_variant(self, url: str) -> bool:
        # Only example.com <-> www.example.com; any other host (e.g. localhost) stays external.
        host = host_of(url)
        start_host = host_of(self.start_url)
        return host != start_host and host.removeprefix("www.") == start_host.removeprefix("www.")

    def _maybe_enqueue(self, link: Link, follow: bool, depth: int) -> None:
        if link.link_type not in _FOLLOWED_TYPES:
            return
        if not follow and not self.config.follow_nofollow:
            return
        if self.config.max_depth >= 0 and depth > self.config.max_depth:
            return
        external = not self.scope.is_internal(link.url)
        if self.scope.should_crawl(link.url) or (external and self.config.check_external):
            self._enqueue(link.url, depth)

    async def _robots_for(self, url: str) -> RobotsRules:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        task = self._robots.get(origin)
        if task is None:
            task = asyncio.create_task(self._load_robots(origin))
            self._robots[origin] = task
        # Shield so one cancelled worker doesn't cancel the fetch other workers are awaiting.
        return await asyncio.shield(task)

    async def _load_robots(self, origin: str) -> RobotsRules:
        assert self._client is not None
        await self._limiter.wait()
        result = await fetch(
            self._client,
            f"{origin}/robots.txt",
            max_bytes=ROBOTS_MAX_BYTES,
            body="always",
            follow_redirects=True,
        )
        code = result.status_code
        if 200 <= code < 300:
            return RobotsRules.parse(result.body.decode("utf-8", "replace"))
        # RFC 9309: an unreachable robots.txt (5xx/network) means "disallow all"; 4xx means allow.
        if (
            code >= 500
            or code == 429
            or (code == 0 and result.error and not result.error.startswith(TOO_MANY_REDIRECTS))
        ):
            logger.warning(
                "%s/robots.txt could not be read (%s), so that host is treated as fully blocked."
                " Use --ignore-robots to crawl it anyway.",
                origin,
                result.status_text or code,
            )
            return RobotsRules.disallow_all()
        return RobotsRules.allow_all()
