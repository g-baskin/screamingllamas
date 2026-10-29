import pytest

from screamingllamas.urls import Scope, normalize_url


@pytest.mark.parametrize(
    ("url", "base", "expected"),
    [
        ("HTTPS://Example.COM", None, "https://example.com/"),
        ("http://example.com:80/a", None, "http://example.com/a"),
        ("https://example.com:8443/a", None, "https://example.com:8443/a"),
        ("/b/../c/./d#frag", "https://example.com/a/", "https://example.com/c/d"),
        ("x/", "https://example.com/a/b", "https://example.com/a/x/"),
        ("https://example.com/a/b/..", None, "https://example.com/a/"),
        ("//cdn.example.com/i.png", "https://example.com/", "https://cdn.example.com/i.png"),
        ("?page=2", "https://example.com/list", "https://example.com/list?page=2"),
        ("/a b/ü?q=a b", "https://example.com/", "https://example.com/a%20b/%C3%BC?q=a%20b"),
        ("/a%2fb", "https://example.com/", "https://example.com/a%2Fb"),
        ("  /x\n/y\t ", "https://example.com/", "https://example.com/x/y"),
        ("https://bücher.example/", None, "https://xn--bcher-kva.example/"),
        ("https://user:pw@example.com/", None, "https://example.com/"),
        ("http://[::1]:8080/", None, "http://[::1]:8080/"),
        ("", "https://example.com/p", "https://example.com/p"),
    ],
)
def test_normalize_url(url: str, base: str | None, expected: str) -> None:
    assert normalize_url(url, base) == expected


@pytest.mark.parametrize(
    "url",
    ["mailto:a@b.c", "tel:123", "javascript:void(0)", "data:text/plain,hi", "ftp://x.com/"],
)
def test_normalize_url_rejects_non_crawlable(url: str) -> None:
    assert normalize_url(url, "https://example.com/") is None


def test_normalize_url_rejects_missing_host() -> None:
    assert normalize_url("https://") is None
    assert normalize_url("not a url") is None


def test_scope_hosts_and_subdomains() -> None:
    scope = Scope("https://www.example.com/")
    assert scope.is_internal("http://www.example.com/a")
    assert not scope.is_internal("https://example.com/")
    assert not scope.is_internal("https://blog.example.com/")

    wide = Scope("https://www.example.com/", include_subdomains=True)
    assert wide.is_internal("https://example.com/")
    assert wide.is_internal("https://blog.example.com/")
    assert not wide.is_internal("https://notexample.com/")


def test_scope_include_exclude_never_drops_start_url() -> None:
    scope = Scope("https://example.com/", include=[r"/blog/"], exclude=[r"/blog/drafts/"])
    assert scope.should_crawl("https://example.com/")
    assert scope.should_crawl("https://example.com/blog/post")
    assert not scope.should_crawl("https://example.com/shop")
    assert not scope.should_crawl("https://example.com/blog/drafts/x")
    assert not scope.should_crawl("https://other.com/blog/")
