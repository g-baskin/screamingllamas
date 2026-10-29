import asyncio

import httpx
from conftest import START, FakeSite, page, redirect, robots

from screamingllamas.config import CrawlConfig
from screamingllamas.crawler import Crawler
from screamingllamas.store import Store

HOME = page(
    """
    <a href="/about">About</a>
    <a href="/old">Old page</a>
    <a href="/missing">Broken</a>
    <a href="/private/secret">Secret</a>
    <a href="/hidden" rel="nofollow">Hidden</a>
    <a href="https://other.com/page">Elsewhere</a>
    <a href="mailto:hi@example.com">Mail</a>
    <img src="/logo.png" alt="Logo">
    """,
    head='<title>Home</title><meta name="description" content="Welcome">'
    '<link rel="canonical" href="/">',
)


def basic_site() -> FakeSite:
    return FakeSite(
        {
            f"{START}robots.txt": robots("User-agent: *\nDisallow: /private/\n"),
            START: HOME,
            f"{START}about": page(
                '<a href="/">Home</a>', head='<title>About</title><link rel="canonical" href="/">'
            ),
            f"{START}old": redirect("/new"),
            f"{START}new": page("<h1>New</h1>", head="<title>New</title>"),
            "https://other.com/robots.txt": robots(""),
            "https://other.com/page": page('<a href="/deeper">Deeper</a>'),
        }
    )


def test_crawl_records_pages_redirects_errors_and_robots(crawl) -> None:
    site = basic_site()
    store = crawl(site)

    home = store.get_url(START)
    assert (home["status_code"], home["title"], home["meta_description"]) == (
        200,
        "Home",
        "Welcome",
    )
    assert (home["indexability"], home["depth"], home["is_internal"]) == ("Indexable", 0, 1)
    assert home["content_hash"]

    about = store.get_url(f"{START}about")
    assert (about["indexability"], about["indexability_status"]) == (
        "Non-Indexable",
        "Canonicalised",
    )

    old = store.get_url(f"{START}old")
    assert (old["status_code"], old["redirect_url"], old["indexability_status"]) == (
        301,
        f"{START}new",
        "Redirected",
    )
    assert store.get_url(f"{START}new")["depth"] == 2

    missing = store.get_url(f"{START}missing")
    assert (missing["status_code"], missing["indexability_status"]) == (404, "Client Error")

    blocked = store.get_url(f"{START}private/secret")
    assert (blocked["status_code"], blocked["status_text"]) == (0, "Blocked by robots.txt")
    assert f"{START}private/secret" not in site.requests

    # Nofollow links, images and external links are recorded but not requested by default.
    for url in (f"{START}hidden", f"{START}logo.png", "https://other.com/page"):
        assert store.get_url(url) is None
        assert url not in site.requests
    links = {row["target_url"]: dict(row) for row in store.query("SELECT * FROM links")}
    assert links[f"{START}hidden"]["follow"] == 0
    assert links["https://other.com/page"]["is_internal"] == 0
    assert links[f"{START}logo.png"]["link_type"] == "image"
    assert site.requests.count(f"{START}robots.txt") == 1


def test_check_external_records_status_without_crawling_further(crawl) -> None:
    site = basic_site()
    store = crawl(site, check_external=True)
    external = store.get_url("https://other.com/page")
    assert (external["status_code"], external["is_internal"], external["title"]) == (200, 0, "")
    assert "https://other.com/deeper" not in site.requests


def test_ignore_robots_crawls_blocked_urls(crawl) -> None:
    site = basic_site()
    store = crawl(site, respect_robots=False)
    assert store.get_url(f"{START}private/secret")["status_code"] == 404
    assert f"{START}robots.txt" not in site.requests


def test_max_depth_and_max_urls(crawl) -> None:
    routes = {f"{START}robots.txt": robots("")}
    routes[START] = page('<a href="/1">1</a>')
    for n in range(1, 5):
        routes[f"{START}{n}"] = page(f'<a href="/{n + 1}">next</a>')
    shallow = crawl(FakeSite(routes), max_depth=1)
    assert [r["url"] for r in shallow.query("SELECT url FROM urls")] == [START, f"{START}1"]

    capped = crawl(FakeSite(routes), max_urls=3)
    assert capped.query("SELECT COUNT(*) FROM urls").fetchone()[0] == 3


def test_robots_server_error_blocks_the_host(crawl) -> None:
    site = FakeSite({f"{START}robots.txt": (503, {}, ""), START: HOME})
    store = crawl(site)
    assert store.get_url(START)["status_text"] == "Blocked by robots.txt"
    assert START not in site.requests


def test_page_level_nofollow_and_noindex(crawl) -> None:
    site = FakeSite(
        {
            START: page(
                '<a href="/a">A</a>', head='<meta name="robots" content="noindex, nofollow">'
            ),
            f"{START}a": page("A"),
        }
    )
    store = crawl(site)
    assert store.get_url(START)["indexability_status"] == "noindex"
    assert store.get_url(f"{START}a") is None
    assert crawl(site, follow_nofollow=True).get_url(f"{START}a")["status_code"] == 200


def test_start_url_redirect_to_www_is_followed(crawl) -> None:
    site = FakeSite(
        {
            START: redirect("https://www.example.com/"),
            "https://www.example.com/": page('<a href="/about">About</a>'),
            "https://www.example.com/about": page("About"),
        }
    )
    store = crawl(site)
    assert store.get_url("https://www.example.com/about")["is_internal"] == 1


def test_exclude_pattern_skips_urls(crawl) -> None:
    site = FakeSite({START: page('<a href="/blog/a">A</a><a href="/shop">S</a>')})
    store = crawl(site, exclude=(r"/blog/",))
    assert store.get_url(f"{START}blog/a") is None
    assert store.get_url(f"{START}shop") is not None


def test_connection_errors_and_non_html_are_recorded(crawl) -> None:
    site = FakeSite(
        {
            START: page('<a href="/down">Down</a><a href="/guide.pdf">PDF</a>'),
            f"{START}down": httpx.ConnectError("refused"),
            f"{START}guide.pdf": (
                200,
                {"content-type": "application/pdf", "content-length": "1234"},
                b"%PDF",
            ),
        }
    )
    store = crawl(site)
    down = store.get_url(f"{START}down")
    assert (down["status_code"], down["status_text"]) == (0, "Connection Error")
    assert "refused" in down["error"]
    pdf = store.get_url(f"{START}guide.pdf")
    assert (pdf["status_code"], pdf["content_type"], pdf["title_count"]) == (
        200,
        "application/pdf",
        0,
    )
    assert pdf["indexability"] == "Indexable"


def test_start_url_redirect_to_another_site_stays_external(crawl) -> None:
    site = FakeSite(
        {
            START: redirect("http://127.0.0.1:8080/"),
            "http://127.0.0.1:8080/": page('<a href="/admin">Admin</a>'),
        }
    )
    store = crawl(site, check_external=True)
    assert store.get_url("http://127.0.0.1:8080/")["is_internal"] == 0
    assert "http://127.0.0.1:8080/admin" not in site.requests


def test_stop_before_run_makes_no_requests(tmp_path) -> None:
    site = FakeSite({START: page("<title>Home</title>")})
    with Store.create(tmp_path / "crawl.db") as store:
        crawler = Crawler(CrawlConfig(start_url=START), store, transport=site.transport)
        crawler.stop()
        stats = asyncio.run(crawler.run())
    assert crawler.stopped and stats.crawled == 0 and site.requests == []
