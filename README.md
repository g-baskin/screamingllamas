# Screaming Llamas

![Screaming Llamas — Crawl. Inspect. Export.](docs/assets/readme-banner.png)

A command-line SEO crawler that works the same way on Intel Macs, Apple Silicon Macs and Windows. It crawls a website and saves every URL and link to a SQLite database. When the crawl ends it exports Screaming Frog-style CSV files.

This is the MVP. More issue reports, JavaScript rendering and integrations are planned for later.

## Install

You need Python 3.11 or newer. On Windows, install it from python.org or the Microsoft Store. On a Mac, use python.org or Homebrew.

For development, install [uv](https://docs.astral.sh/uv/) and run this from this folder:

```sh
uv sync
uv run screamingllamas --help
```

To use the tool from any folder without a development setup:

```sh
uv tool install .        # or: pipx install .   or: pip install .
screamingllamas --help
```

The commands are the same in Terminal, PowerShell and Command Prompt.

## Crawl a site

```sh
screamingllamas crawl https://example.com --max-urls 500
```

If you leave out the scheme, `https://` is added for you. Results go to `./crawls/<host>-<date>-<time>/` unless you pass `-o FOLDER`. Press Ctrl+C to stop early. Everything crawled up to that point is kept and exported.

| Option | Default | What it does |
|---|---|---|
| `--max-urls N` | 0 (no limit) | Stop after N URLs |
| `--max-depth N` | -1 (no limit) | Only follow links up to N clicks from the start URL |
| `--concurrency N` | 5 | Number of requests running at once (1 to 100) |
| `--rate R` | 0 (no limit) | Send at most R requests per second |
| `--timeout S` | 20 | Seconds to wait for each response |
| `--user-agent UA` | `ScreamingLlamas/<version> (SEO crawler)` | The User-Agent header sent. Its first word is also the name matched in robots.txt. |
| `--ignore-robots` | off | Crawl URLs that robots.txt blocks |
| `--follow-nofollow` | off | Follow nofollow, ugc and sponsored links, and links on pages marked nofollow |
| `--include-subdomains` | off | Treat `blog.example.com` and similar hosts as internal |
| `--check-external` | off | Request external links to get their status codes; their pages are not crawled |
| `--include REGEX` | — | Only crawl internal URLs that match (you can repeat this) |
| `--exclude REGEX` | — | Skip URLs that match (you can repeat this) |
| `-q`, `--quiet` | off | Hide the progress lines |

## Output

| File | Contents |
|---|---|
| `crawl.db` | SQLite database with a `urls` table (one row per URL), a `links` table (one row per link) and a `meta` table (crawl settings and status) |
| `internal_all.csv` | One row per internal URL, with the columns listed below |
| `external_all.csv` | External URLs, with inlink counts and, with `--check-external`, status codes |
| `all_inlinks.csv` | Every hyperlink, image, canonical, hreflang, rel next/prev, redirect and meta refresh found |

`internal_all.csv` has these columns: status code, indexability, title, meta description, H1, H2, meta robots, X-Robots-Tag, canonical, size, word count, language, crawl depth, inlinks, outlinks, response time, redirect target and content hash.

To write the CSVs again from a saved crawl:

```sh
screamingllamas export crawls/example.com-20261028-210000/crawl.db --force
```

The CSVs are saved as UTF-8 with a BOM so Excel on Windows shows accents correctly. Any cell that starts with `=`, `+`, `-` or `@` gets a leading `'` so spreadsheet apps can't run crawled text as a formula.

## Desktop app

There is also a desktop window for Mac and Windows. It runs crawls, shows the results while they come in, and opens saved crawls. It needs the optional `ui` extra:

```sh
uv sync --extra ui
uv run screamingllamas ui                      # new crawls go to ./crawls/
uv run screamingllamas ui crawls/example.com-20261028-210000/crawl.db
```

Add `--crawls-dir FOLDER` to save new crawls somewhere other than `./crawls/`.

To use the desktop app from any folder, install it from this folder with the extra. The package is not on PyPI, so don't install it by name:

```sh
uv tool install ".[ui]"     # or: pipx install ".[ui]"   or: pip install ".[ui]"
screamingllamas ui
```

- **Start / Stop.** Stop keeps everything crawled so far, the same as Ctrl+C. Closing the window also stops the crawl and saves it.
- **Settings** has the same options as the `crawl` command.
- **Internal / External tabs** show one page of 200 URLs at a time, so very large crawls stay fast. Click a column to sort it. Use **Show** for filters such as client errors, missing titles, duplicate titles or meta descriptions, and more than one H1. Search matches part of the address.
- **The bottom pane** shows the selected URL's fields, inlinks, outlinks and response headers.
- Each crawl still writes `crawl.db` and the three CSVs to its folder. **Show folder** opens it.

The window uses the web view built into the system: WKWebView on macOS and Microsoft Edge WebView2 on Windows. Windows 11 and up-to-date Windows 10 already have WebView2; if the window stays blank, install the WebView2 Runtime from Microsoft. On Windows, `pywebview` also installs `pythonnet`, which uses the .NET Framework that comes with Windows.

## How a crawl works

1. The start URL goes into a queue. URLs are normalised: the fragment and default port are removed, the host is lowercased and `.`/`..` segments are resolved. Each URL is crawled only once.
2. robots.txt is fetched once per host and matched the way RFC 9309 and Google describe: `*` and `$` wildcards, longest rule wins. A 4xx means everything is allowed. A 5xx or network error means the host is treated as fully blocked.
3. Redirects are not followed automatically. Each hop is saved as its own row and the target is queued.
4. Internal HTML pages are parsed with selectolax (an HTML5 parser) to pull out the SEO fields and links. Pages larger than 10 MB are cut off at 10 MB.
5. Indexability is decided from the status code, robots.txt, noindex in meta robots or X-Robots-Tag, and whether the canonical points to a different URL.

## Development

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Code lives in `src/screamingllamas/`: `cli`, `config`, `crawler`, `fetch`, `extract`, `robots`, `urls`, `store` and `export`. The desktop app is in `ui/`: `session.py` runs crawls on a background thread and answers the window's queries, `app.py` opens the window, and `static/` holds the page (plain HTML, CSS and JavaScript, no build step).

## Licence

Proprietary, all rights reserved. See [LICENSE](LICENSE).
