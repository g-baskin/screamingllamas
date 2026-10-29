from __future__ import annotations

import re
from dataclasses import dataclass, field

from selectolax.lexbor import LexborHTMLParser, LexborNode

from .urls import normalize_url

_NOFOLLOW_RELS = frozenset({"nofollow", "ugc", "sponsored"})
_NON_CONTENT_TAGS = ["script", "style", "noscript", "template", "svg"]
_REFRESH = re.compile(r"^\s*[\d.]*\s*[;,]?\s*(?:url\s*=\s*)?(.*)$", re.I | re.S)


@dataclass(slots=True)
class Link:
    url: str
    link_type: str
    anchor: str = ""
    alt: str | None = None
    rel: str = ""
    hreflang: str = ""

    @property
    def nofollow(self) -> bool:
        return not _NOFOLLOW_RELS.isdisjoint(self.rel.split())


@dataclass(slots=True)
class PageData:
    titles: list[str] = field(default_factory=list)
    meta_descriptions: list[str] = field(default_factory=list)
    meta_robots: list[str] = field(default_factory=list)
    canonicals: list[str] = field(default_factory=list)
    h1: list[str] = field(default_factory=list)
    h2: list[str] = field(default_factory=list)
    lang: str = ""
    word_count: int = 0
    meta_refresh: str | None = None
    links: list[Link] = field(default_factory=list)


def _clean(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _attr(node: LexborNode, name: str) -> str:
    return node.attributes.get(name) or ""


def _in_svg(node: LexborNode) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.tag == "svg":
            return True
        parent = parent.parent
    return False


def _refresh_target(content: str, base_url: str) -> str | None:
    match = _REFRESH.match(content)
    raw = match.group(1).strip().strip("'\"") if match else ""
    return normalize_url(raw, base_url) if raw else None


def extract_page(html: str, page_url: str) -> PageData:
    tree = LexborHTMLParser(html)
    data = PageData()

    root = tree.css_first("html")
    if root is not None:
        data.lang = _clean(_attr(root, "lang"))

    base_url = page_url
    base = tree.css_first("base[href]")
    if base is not None:
        base_url = normalize_url(_attr(base, "href"), page_url) or page_url

    # <title> inside inline SVG labels the graphic, not the document.
    data.titles = [_clean(node.text()) for node in tree.css("title") if not _in_svg(node)]

    for meta in tree.css("meta"):
        name = _attr(meta, "name").strip().lower()
        if name == "description":
            data.meta_descriptions.append(_clean(_attr(meta, "content")))
        elif name == "robots":
            data.meta_robots.append(_clean(_attr(meta, "content")))
        elif _attr(meta, "http-equiv").strip().lower() == "refresh" and data.meta_refresh is None:
            target = _refresh_target(_attr(meta, "content"), base_url)
            if target and target != page_url:
                data.meta_refresh = target
                data.links.append(Link(target, "meta_refresh"))

    data.h1 = [_clean(node.text(separator=" ")) for node in tree.css("h1")]
    data.h2 = [_clean(node.text(separator=" ")) for node in tree.css("h2")]

    for node in tree.css("link[href]"):
        target = normalize_url(_attr(node, "href"), base_url)
        if target is None:
            continue
        rels = _attr(node, "rel").lower().split()
        if "canonical" in rels:
            data.canonicals.append(target)
            data.links.append(Link(target, "canonical"))
        if "alternate" in rels and _attr(node, "hreflang"):
            data.links.append(Link(target, "hreflang", hreflang=_attr(node, "hreflang").strip()))
        if "next" in rels:
            data.links.append(Link(target, "next"))
        if "prev" in rels or "previous" in rels:
            data.links.append(Link(target, "prev"))

    for node in tree.css("a[href], area[href]"):
        target = normalize_url(_attr(node, "href"), base_url)
        if target is None:
            continue
        alt: str | None = None
        image = node.css_first("img")
        if node.tag == "area":
            alt = _clean(_attr(node, "alt"))
        elif image is not None and "alt" in image.attributes:
            alt = _clean(_attr(image, "alt"))
        data.links.append(
            Link(
                target,
                "hyperlink",
                anchor=_clean(node.text(separator=" ")),
                alt=alt,
                rel=" ".join(_attr(node, "rel").lower().split()),
            )
        )

    for node in tree.css("img[src]"):
        target = normalize_url(_attr(node, "src"), base_url)
        if target is not None:
            alt = _clean(_attr(node, "alt")) if "alt" in node.attributes else None
            data.links.append(Link(target, "image", alt=alt))

    # Done reading the tree, so strip non-visible elements in place for the word count.
    tree.strip_tags(_NON_CONTENT_TAGS)
    if tree.body is not None:
        data.word_count = len(tree.body.text(separator=" ").split())
    return data
