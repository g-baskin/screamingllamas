from __future__ import annotations

import logging
from pathlib import Path

import webview

from .. import __version__
from .session import CrawlSession, make_api

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).with_name("static")


def launch(session: CrawlSession) -> None:
    windows: list[webview.Window] = []

    def choose_database() -> Path | None:
        picked = windows[0].create_file_dialog(
            webview.FileDialog.OPEN, file_types=("Crawl database (*.db)", "All files (*.*)")
        )
        return Path(picked[0]) if picked else None

    windows.append(
        webview.create_window(
            f"Screaming Llamas {__version__}",
            url=str(STATIC_DIR / "index.html"),
            js_api=make_api(session, choose_database),
            width=1280,
            height=820,
            min_size=(960, 600),
            text_select=True,
        )
    )
    try:
        webview.start(private_mode=True)
    finally:
        if session.is_busy():
            logger.warning("Window closed: stopping the crawl and saving what was crawled...")
        session.shutdown()
