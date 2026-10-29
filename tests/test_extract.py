from screamingllamas.extract import extract_page
from screamingllamas.fetch import decode_html

PAGE = """<!doctype html>
<html lang="en-GB">
<head>
  <base href="https://example.com/shop/">
  <title>  Llama   Shop </title>
  <meta name="Description" content="Buy llamas">
  <meta name="description" content="Second description">
  <meta name="robots" content="noindex, follow">
  <link rel="canonical" href="llamas#top">
  <link rel="alternate" hreflang="fr" href="/fr/">
  <link rel="next" href="?page=2">
  <style>p { color: red }</style>
</head>
<body>
  <svg><title>icon label</title></svg>
  <h1>Llamas <em>for sale</em></h1><h1>Second H1</h1>
  <h2>Prices</h2>
  <p>One two three four.</p>
  <a href="about">About us</a>
  <a href="/sponsor" rel="Sponsored noopener">Partner</a>
  <a href="https://other.com/"><img src="/logo.png" alt="Logo"></a>
  <a href="mailto:hi@example.com">Mail</a>
  <img src="/no-alt.png"><img src="/empty-alt.png" alt>
  <script>var hidden = "five six";</script>
</body>
</html>"""


def test_extract_page_reads_seo_fields() -> None:
    data = extract_page(PAGE, "https://example.com/shop/index.html")
    assert data.titles == ["Llama Shop"]
    assert data.meta_descriptions == ["Buy llamas", "Second description"]
    assert data.meta_robots == ["noindex, follow"]
    assert data.canonicals == ["https://example.com/shop/llamas"]
    assert data.h1 == ["Llamas for sale", "Second H1"]
    assert data.h2 == ["Prices"]
    assert data.lang == "en-GB"
    # Visible body text only: h1s (5), h2 (1), paragraph (4), link text (4); script/svg excluded.
    assert data.word_count == 14


def test_extract_page_collects_links() -> None:
    links = {
        (link.link_type, link.url): link
        for link in extract_page(PAGE, "https://example.com/").links
    }
    assert ("hyperlink", "https://example.com/shop/about") in links
    sponsor = links[("hyperlink", "https://example.com/sponsor")]
    assert sponsor.nofollow and sponsor.rel == "sponsored noopener"
    logo_link = links[("hyperlink", "https://other.com/")]
    assert logo_link.anchor == "" and logo_link.alt == "Logo"
    assert links[("image", "https://example.com/no-alt.png")].alt is None
    assert links[("image", "https://example.com/empty-alt.png")].alt == ""
    assert links[("hreflang", "https://example.com/fr/")].hreflang == "fr"
    assert ("next", "https://example.com/shop/?page=2") in links
    assert not any(url.startswith("mailto:") for _, url in links)


def test_meta_refresh_becomes_a_link() -> None:
    html = '<meta http-equiv="Refresh" content="0; URL=\'/new-home\'">'
    data = extract_page(html, "https://example.com/")
    assert data.meta_refresh == "https://example.com/new-home"
    assert [(link.link_type, link.url) for link in data.links] == [
        ("meta_refresh", "https://example.com/new-home")
    ]
    assert (
        extract_page('<meta http-equiv="refresh" content="300">', "https://e.com/").meta_refresh
        is None
    )


def test_decode_html_charsets() -> None:
    assert decode_html("café".encode("cp1252"), "text/html; charset=ISO-8859-1") == "café"
    meta = b'<meta charset="windows-1252"><p>caf\xe9</p>'
    assert "café" in decode_html(meta, "text/html")
    assert decode_html(b"\xef\xbb\xbfcaf\xc3\xa9", "text/html; charset=latin1") == "café"
    assert decode_html("café".encode(), "text/html; charset=base64") == "café"
    assert decode_html("café".encode(), "text/html; charset=nonsense") == "café"
    assert decode_html("café".encode(), "text/html; charset=idna") == "café"
