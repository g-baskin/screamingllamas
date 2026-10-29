import asyncio
import re
import sys
import time
from pathlib import Path

import httpx
import pytest
from conftest import START, FakeSite, page

from screamingllamas.cli import main
from screamingllamas.export import EXPORT_FILES
from screamingllamas.store import Store
from screamingllamas.ui import session as session_module
from screamingllamas.ui.session import CrawlSession, make_api, open_in_browser


def small_site() -> FakeSite:
    return FakeSite(
        {
            START: page(
                '<a href="/a">A</a><a href="/b">B</a><a href="/c">C</a>'
                '<a href="https://other.com/x">X</a>',
                head="<title>Home</title>",
            ),
            f"{START}a": page('<a href="/">Home</a><a href="/b">B</a>', head="<title>Dup</title>"),
            f"{START}b": page("<h1>B</h1>", head="<title>Dup</title>"),
            f"{START}c": page("<p>No title</p>"),
        }
    )


def finished_session(tmp_path: Path) -> tuple[CrawlSession, Path]:
    session = CrawlSession(tmp_path, transport=small_site().transport)
    started = session.start({"url": "example.com", "concurrency": "2"})
    assert started["ok"], started
    assert session.wait(10)
    return session, Path(started["folder"])


def urls(result: dict) -> list[str]:
    return [row[0] for row in result["rows"]]


def test_session_crawls_exports_and_serves_sorted_filtered_pages(tmp_path: Path) -> None:
    session, folder = finished_session(tmp_path)
    status = session.status()
    assert (status["state"], status["result"], status["crawled"]) == ("done", "completed", 4)
    assert status["folder"] == str(folder) and folder.parent == tmp_path
    assert all((folder / name).exists() for name in EXPORT_FILES)

    first = session.rows("internal", {"limit": 2, "sort": "inlinks", "desc": True})
    keys = [column["key"] for column in first["columns"]]
    assert first["total"] == 4 and len(first["rows"]) == 2
    assert first["rows"][0][0] == f"{START}b" and first["rows"][0][keys.index("inlinks")] == 2
    second = session.rows("internal", {"limit": 2, "offset": 2, "sort": "inlinks", "desc": True})
    assert len(set(urls(first)) | set(urls(second))) == 4

    assert sorted(urls(session.rows("internal", {"filter": "duplicate_title"}))) == [
        f"{START}a",
        f"{START}b",
    ]
    assert urls(session.rows("internal", {"filter": "missing_title"})) == [f"{START}c"]
    assert urls(session.rows("internal", {"search": "/C"})) == [f"{START}c"]
    # LIKE wildcards in the search box are literal characters.
    assert session.rows("internal", {"search": "%"})["total"] == 0

    external = session.rows("external", {"filter": "not_checked"})
    assert external["rows"] == [["https://other.com/x", None, None, None, 1]]


def test_url_info_lists_details_headers_and_links(tmp_path: Path) -> None:
    session, _ = finished_session(tmp_path)
    info = session.url_info(f"{START}b")
    assert info["crawled"] and ["Title 1", "Dup"] in info["details"]
    assert ["content-type", "text/html; charset=utf-8"] in info["headers"]
    assert info["inlinks"]["total"] == 2
    assert {row[1] for row in info["inlinks"]["rows"]} == {START, f"{START}a"}
    assert session.url_info(START)["outlinks"]["total"] == 4

    external = session.url_info("https://other.com/x")
    assert not external["crawled"] and external["details"] == []
    assert external["inlinks"]["rows"] == [["Hyperlink", START, "X", None, "", "Yes"]]


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"url": "  "}, "Enter the address"),
        ({"url": "ftp://example.com"}, "not an http or https address"),
        ({"url": "example.com", "concurrency": "0"}, "concurrency must be between 1 and 100"),
        ({"url": "example.com", "max_urls": "2.5"}, "Max URLs must be a whole number"),
        ({"url": "example.com", "rate": "nan"}, "Rate must be a number"),
        ({"url": "example.com", "timeout": True}, "Timeout must be a number"),
        ({"url": "example.com", "exclude": ["("]}, "invalid regex"),
        ("example.com", "must be an object"),
    ],
)
def test_start_rejects_bad_options_before_creating_anything(
    tmp_path: Path, options: object, message: str
) -> None:
    session = CrawlSession(tmp_path / "crawls")
    result = session.start(options)
    assert not result["ok"] and message in result["error"]
    assert not (tmp_path / "crawls").exists()
    assert session.status()["state"] == "idle"


@pytest.mark.parametrize(
    ("tab", "query"),
    [
        ("nope", {}),
        ("internal", {"sort": "u.url; DROP TABLE urls"}),
        ("internal", {"filter": "1 = 1"}),
        ("internal", {"offset": -1}),
        ("internal", {"limit": True}),
        ("internal", {"search": 5}),
        ("internal", "not an object"),
    ],
)
def test_rows_rejects_anything_outside_the_whitelist(tmp_path: Path, tab, query) -> None:
    with pytest.raises(ValueError):
        CrawlSession(tmp_path).rows(tab, query)


def test_rows_before_any_crawl_are_empty(tmp_path: Path) -> None:
    result = CrawlSession(tmp_path).rows("internal", {})
    assert (result["rows"], result["total"], result["has_crawl"]) == ([], 0, False)


def test_stop_ends_the_crawl_quickly_and_keeps_partial_results(tmp_path: Path) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/":
            html = '<title>Home</title><a href="/slow">Slow</a>'
            return httpx.Response(200, headers={"content-type": "text/html"}, text=html)
        if request.url.path == "/slow":
            await asyncio.sleep(60)  # never answers in time; Stop must not wait for it
        return httpx.Response(404)

    session = CrawlSession(tmp_path, transport=httpx.MockTransport(handler))
    started = session.start({"url": START})
    assert started["ok"]
    deadline = time.monotonic() + 10
    while session.status().get("crawled", 0) < 1:
        assert time.monotonic() < deadline
        time.sleep(0.02)

    assert "already running" in session.start({"url": START})["error"]
    assert "Stop the running crawl" in session.open_db(tmp_path / "other.db")["error"]
    assert session.stop()["ok"]
    assert session.wait(10)

    status = session.status()
    assert (status["state"], status["result"]) == ("done", "interrupted")
    assert status["elapsed"] < 10
    folder = Path(started["folder"])
    with Store.open(folder / "crawl.db") as store:
        assert store.get_meta()["status"] == "interrupted"
    assert all((folder / name).exists() for name in EXPORT_FILES)
    assert urls(session.rows("internal", {})) == [START]
    assert not session.stop()["ok"]


def test_open_db_shows_a_saved_crawl_and_rejects_other_files(tmp_path: Path, monkeypatch) -> None:
    _, folder = finished_session(tmp_path / "first")
    viewer = CrawlSession(tmp_path / "crawls")
    monkeypatch.chdir(tmp_path)
    assert viewer.open_db(folder.relative_to(tmp_path) / "crawl.db") == {"ok": True}
    status = viewer.status()
    assert status["folder"] == str(folder.resolve())
    assert (status["state"], status["result"], status["start_url"]) == (
        "opened",
        "completed",
        START,
    )
    assert (status["crawled"], status["blocked"]) == (4, 0)
    assert viewer.rows("internal", {})["total"] == 4

    other = tmp_path / "notes.db"
    other.write_bytes(b"not a database")
    result = CrawlSession(tmp_path).open_db(other)
    assert not result["ok"] and "not a Screaming Llamas crawl database" in result["error"]
    assert other.read_bytes() == b"not a database"


def test_js_api_exposes_methods_but_no_reachable_state(tmp_path: Path) -> None:
    api = make_api(CrawlSession(tmp_path), lambda: None)
    assert vars(api) == {}
    assert all(callable(getattr(api, name)) for name in dir(api) if not name.startswith("_"))
    assert api.open_crawl() == {"ok": False, "cancelled": True}
    assert api.status()["state"] == "idle"
    assert set(api.reports()) == {"internal", "external"}


def test_open_in_browser_only_opens_http_urls(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(session_module.webbrowser, "open", opened.append)
    for bad in ("javascript:alert(1)", "file:///etc/passwd", "https://", 42):
        assert not open_in_browser(bad)["ok"]
    assert open_in_browser(START) == {"ok": True}
    assert opened == [START]


def test_ui_command_rejects_a_bad_database(tmp_path: Path, capsys) -> None:
    assert main(["ui", str(tmp_path / "missing.db")]) == 1
    assert "does not exist" in capsys.readouterr().err


def test_ui_command_explains_how_to_install_the_extra(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setitem(sys.modules, "webview", None)
    monkeypatch.delitem(sys.modules, "screamingllamas.ui.app", raising=False)
    assert main(["ui", "--crawls-dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    # Not on PyPI: the hint must point at the local folder, never at a package name.
    assert "'ui' extra" in err and '".[ui]"' in err and "screamingllamas[ui]" not in err


STATIC = Path(session_module.__file__).with_name("static")


def test_page_csp_allows_the_pywebview_bridge_but_nothing_remote() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    csp = re.search(r'http-equiv="Content-Security-Policy" content="([^"]+)"', html)
    assert csp is not None
    directives = dict(part.strip().split(" ", 1) for part in csp.group(1).split(";"))
    # pywebview delivers every Python return value to the page through eval().
    assert directives["script-src"] == "'self' 'unsafe-eval'"
    assert directives["default-src"] == "'none'"
    assert "http" not in csp.group(1) and "connect-src" not in directives


def test_every_element_the_script_uses_exists_in_the_page() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    script = (STATIC / "app.js").read_text(encoding="utf-8")
    used = set(re.findall(r'byId\("([\w-]+)"\)', script))
    assert used and used <= set(re.findall(r'id="([\w-]+)"', html))
    assert ".innerHTML" not in script and "eval(" not in script
