import csv
from pathlib import Path

from conftest import START, FakeSite, page

from screamingllamas.export import EXTERNAL_FILE, INLINKS_FILE, INTERNAL_FILE, export_crawl


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def test_export_writes_internal_external_and_inlinks(crawl, tmp_path: Path) -> None:
    site = FakeSite(
        {
            START: page(
                '<a href="/a">A</a><a href="/a">A again</a>'
                '<a href="https://other.com/" rel="ugc">X</a>',
                head='<title>=HYPERLINK("http://evil")</title>',
            ),
            f"{START}a": page('<a href="/">Home</a>', head="<title>Page A</title>"),
        }
    )
    store = crawl(site)
    written = dict((path.name, rows) for path, rows in export_crawl(store, tmp_path / "out"))
    assert written == {INTERNAL_FILE: 2, EXTERNAL_FILE: 1, INLINKS_FILE: 4}

    internal = {row["Address"]: row for row in read_csv(tmp_path / "out" / INTERNAL_FILE)}
    home, a = internal[START], internal[f"{START}a"]
    # Crawled titles must not run as spreadsheet formulas.
    assert home["Title 1"] == '\'=HYPERLINK("http://evil")'
    assert (home["Inlinks"], home["Outlinks"], home["External Outlinks"]) == ("1", "2", "1")
    assert (a["Inlinks"], a["Unique Inlinks"], a["Crawl Depth"]) == ("2", "1", "1")

    external = read_csv(tmp_path / "out" / EXTERNAL_FILE)
    assert external == [
        {
            "Address": "https://other.com/",
            "Status Code": "",
            "Status": "",
            "Content Type": "",
            "Size (bytes)": "",
            "Response Time (ms)": "",
            "Inlinks": "1",
        }
    ]

    inlinks = read_csv(tmp_path / "out" / INLINKS_FILE)
    ugc = next(row for row in inlinks if row["Destination"] == "https://other.com/")
    assert (ugc["Type"], ugc["Follow"], ugc["Rel"], ugc["Source"]) == (
        "Hyperlink",
        "false",
        "ugc",
        START,
    )
