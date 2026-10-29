from pathlib import Path

import pytest
from conftest import START, FakeSite, page

from screamingllamas.cli import default_output_dir, main, run_crawl
from screamingllamas.config import CrawlConfig
from screamingllamas.export import INTERNAL_FILE
from screamingllamas.store import Store


def make_crawl(tmp_path: Path) -> Path:
    db = tmp_path / "crawl.db"
    site = FakeSite({START: page("<title>Home</title>")})
    stats, status = run_crawl(CrawlConfig(start_url=START), db, transport=site.transport)
    assert (stats.crawled, status) == (1, "completed")
    return db


def test_run_crawl_saves_metadata(tmp_path: Path) -> None:
    db = make_crawl(tmp_path)
    with Store.open(db) as store:
        meta = store.get_meta()
    assert (meta["status"], meta["start_url"]) == ("completed", START)
    assert meta["finished_at"]


def test_export_refuses_to_overwrite_without_force(tmp_path: Path, capsys) -> None:
    db = make_crawl(tmp_path)
    assert main(["export", str(db)]) == 0
    assert (tmp_path / INTERNAL_FILE).exists()
    assert main(["export", str(db)]) == 1
    assert "--force" in capsys.readouterr().err
    assert main(["export", str(db), "--force"]) == 0


def test_export_rejects_files_that_are_not_crawls(tmp_path: Path, capsys) -> None:
    other = tmp_path / "notes.db"
    other.write_bytes(b"not a database")
    assert main(["export", str(other), "-o", str(tmp_path / "out")]) == 1
    assert "not a Screaming Llamas crawl database" in capsys.readouterr().err
    assert other.read_bytes() == b"not a database"


@pytest.mark.parametrize(
    "args",
    [
        ["crawl", "ftp://example.com"],
        ["crawl", "example.com", "--exclude", "("],
        ["crawl", "example.com", "--concurrency", "0"],
    ],
)
def test_crawl_rejects_bad_input_before_any_request(args: list[str], tmp_path: Path) -> None:
    assert main([*args, "-o", str(tmp_path / "out")]) == 2
    assert not (tmp_path / "out").exists()


def test_crawl_refuses_a_folder_that_already_has_a_crawl(tmp_path: Path, capsys) -> None:
    make_crawl(tmp_path)
    assert main(["crawl", "example.com", "-o", str(tmp_path)]) == 1
    assert "choose another --output" in capsys.readouterr().err


def test_default_output_dir_is_safe_on_windows(tmp_path: Path) -> None:
    first = default_output_dir("http://[::1]:8080/", tmp_path)
    assert ":" not in first.name and first.name.startswith("__1-")
    first.mkdir()
    assert default_output_dir("http://[::1]:8080/", tmp_path) != first
