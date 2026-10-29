from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from screamingllamas.config import CrawlConfig
from screamingllamas.crawler import Crawler
from screamingllamas.store import Store

Route = tuple[int, dict[str, str], str | bytes]
START = "https://example.com/"


def page(body: str, head: str = "") -> Route:
    html = f"<!doctype html><html><head>{head}</head><body>{body}</body></html>"
    return 200, {"content-type": "text/html; charset=utf-8"}, html


def redirect(location: str, status: int = 301) -> Route:
    return status, {"location": location}, ""


def robots(text: str) -> Route:
    return 200, {"content-type": "text/plain"}, text


class FakeSite:
    def __init__(self, routes: dict[str, Route | Exception]) -> None:
        self.routes = routes
        self.requests: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        route = self.routes.get(url, (404, {"content-type": "text/html"}, "<h1>Not found</h1>"))
        if isinstance(route, Exception):
            raise route
        status, headers, body = route
        content = body.encode() if isinstance(body, str) else body
        return httpx.Response(status, headers=headers, content=content)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture
def crawl(tmp_path: Path):
    stores: list[Store] = []

    def run(site: FakeSite, **options: Any) -> Store:
        config = CrawlConfig(start_url=options.pop("start_url", START), **options)
        store = Store.create(tmp_path / f"crawl{len(stores)}.db")
        stores.append(store)
        asyncio.run(Crawler(config, store, transport=site.transport).run())
        return store

    yield run
    for store in stores:
        store.close()
