from __future__ import annotations

import re
from dataclasses import dataclass

from . import __version__

DEFAULT_USER_AGENT = f"ScreamingLlamas/{__version__} (SEO crawler)"


@dataclass(frozen=True, slots=True)
class CrawlConfig:
    start_url: str
    concurrency: int = 5
    max_urls: int = 0
    max_depth: int = -1
    rate_limit: float = 0.0
    timeout: float = 20.0
    user_agent: str = DEFAULT_USER_AGENT
    respect_robots: bool = True
    follow_nofollow: bool = False
    include_subdomains: bool = False
    check_external: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    max_page_bytes: int = 10 * 1024 * 1024

    def __post_init__(self) -> None:
        if not 1 <= self.concurrency <= 100:
            raise ValueError("concurrency must be between 1 and 100")
        if self.max_urls < 0:
            raise ValueError("max URLs must be 0 (no limit) or more")
        if self.max_depth < -1:
            raise ValueError("max depth must be -1 (no limit) or more")
        if self.rate_limit < 0:
            raise ValueError("rate must be 0 (no limit) or more")
        if self.timeout <= 0:
            raise ValueError("timeout must be more than 0 seconds")
        if self.max_page_bytes <= 0:
            raise ValueError("max page bytes must be more than 0")
        if not self.user_agent.strip() or any(ord(c) < 32 for c in self.user_agent):
            raise ValueError("user agent must be non-empty and contain no control characters")
        for pattern in (*self.include, *self.exclude):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
