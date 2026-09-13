# Google-Maps-Scrapper

Scrapes business listings from Google Maps with [Playwright](https://playwright.dev/python/)
and writes them to Excel. Each listing yields a name, address, phone number,
website, plus code, latitude/longitude, rating, review count and category.

> **Terms of service.** Scraping Google Maps is contrary to Google's Terms of
> Service. This project is provided for education and personal experimentation.
> For production or commercial work, use the
> [Places API](https://developers.google.com/maps/documentation/places/web-service/overview),
> which is the supported route for this data. Scrape at a considerate rate and
> at your own risk.

## Table of Contents

- [Requirements](#requirements)
- [Installation](#installation)
- [Usage](#usage)
- [Configuration](#configuration)
- [Output](#output)
- [Modules](#modules)
- [Docker](#docker)
- [Optional SFTP upload](#optional-sftp-upload)
- [Troubleshooting](#troubleshooting)
- [Email scraper](#email-scraper)

## Requirements

- **Python 3.10 or newer.** (Earlier releases of this README claimed a version
  *below* 3.10 was required; that was never accurate, and current Playwright
  requires 3.10+.)
- No system Chrome needed. `playwright install chromium` downloads a matched
  browser build.

## Installation

```bash
git clone https://github.com/r7avi/Google-Maps-Scrapper.git
cd Google-Maps-Scrapper

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
```

## Usage

Search terms come from `Query.txt` (one per line; blank lines and lines starting
with `#` are ignored) or from the command line.

```bash
# Prompt for a term, or read Query.txt
python run.py

# One or more terms straight from the command line
python run.py --search "Plumbers in London" --search "Dentist in New York, USA"

# Work through Query.txt without prompting, 50 results per term, no window
python run.py --non-interactive --total 50 --headless
```

A term is removed from `Query.txt` **only after** its results have been written,
so an interrupted or failed run resumes where it stopped instead of losing the
query. Use `--keep-queries` to leave the file untouched. Terms passed with
`--search` never modify `Query.txt`.

`run.py` exits `0` on success, `1` if any term failed, and `2` on a
configuration problem, which makes it usable from a scheduler or CI job.

### Options

| Flag | Default | Purpose |
| --- | --- | --- |
| `--search TERM` | – | Term to scrape; repeatable. Bypasses the query file. |
| `--query-file PATH` | `Query.txt` | File of search terms. |
| `--total N` | `25` | Listings to target per term. 20–120 is realistic. |
| `--output-dir PATH` | `output` | Where workbooks are written. |
| `--headless` / `--no-headless` | `--no-headless` | Hide or show the browser. |
| `--channel NAME` | – | Use an installed browser, e.g. `--channel chrome`. |
| `--executable-path PATH` | – | Explicit browser binary. Rarely needed. |
| `--max-scroll-attempts N` | `10` | Give up scrolling after N attempts with no new results. |
| `--merge` / `--no-merge` | `--merge` | Combine per-term workbooks into `merged_output.xlsx`. |
| `--keep-queries` | off | Do not remove scraped terms from the query file. |
| `--upload` | off | Send results over SFTP. See below. |
| `--non-interactive` | off | Never prompt. |
| `--log-level LEVEL` | `INFO` | Console verbosity. `scraper.log` always gets DEBUG. |

## Configuration

Every flag has an environment-variable equivalent, which is what the Docker
setup uses. Command-line flags win over environment variables.

| Variable | Flag |
| --- | --- |
| `GMAPS_TOTAL` | `--total` |
| `GMAPS_HEADLESS` | `--headless` |
| `GMAPS_OUTPUT_DIR` | `--output-dir` |
| `GMAPS_QUERY_FILE` | `--query-file` |
| `GMAPS_BROWSER_CHANNEL` | `--channel` |
| `GMAPS_CHROME_PATH` | `--executable-path` |
| `GMAPS_MAX_SCROLL_ATTEMPTS` | `--max-scroll-attempts` |
| `GMAPS_MERGE` | `--merge` / `--no-merge` |
| `GMAPS_CONSUME_QUERIES` | inverse of `--keep-queries` |
| `GMAPS_UPLOAD` | `--upload` |
| `GMAPS_NAV_RETRIES`, `GMAPS_NAV_TIMEOUT_MS`, `GMAPS_BLOCK_IMAGES` | – |

## Output

One workbook per search term in `output/`, named after a sanitised form of the
term, plus `merged_output.xlsx` when merging is on. Columns:

`Names`, `Address`, `Plus Code`, `Phone Number`, `Website`, `Google Link`,
`Latitude`, `Longitude`, `Reviews_Count`, `Average Rates`, `Type`

`Latitude`, `Longitude`, `Reviews_Count` and `Average Rates` are written as
numbers, so they sort and aggregate in Excel. Rows are de-duplicated on
`Google Link`, both per term and across the merge. Listing URLs that could not
be scraped are appended to `failed_links.log` for a later retry.

If nothing was scraped for a term, **no file is written** — an empty workbook
would be indistinguishable from a successful run.

## Modules

| File | Responsibility |
| --- | --- |
| `run.py` | CLI entry point, logging, and the per-term scrape/save/dequeue loop. |
| `config.py` | `Settings`, resolved from CLI flags and environment variables. |
| `models.py` | The `Listing` record, the Excel column order, and value parsers. |
| `scraper.py` | Search, results-feed scrolling, and per-listing extraction. |
| `playwright_helpers.py` | Defensive locator reads, retrying navigation, block detection. |
| `utils.py` | Query-file handling and Excel read/write/merge. |
| `uploader.py` | Optional SFTP upload. |

A listing is a single `Listing` object, not a set of parallel lists. That is
deliberate: the previous design kept every field in its own list and related
rows only by list position, so one failed field silently shifted every following
row into the wrong record.

## Docker

Runs headless, using the same modules as a local checkout. The build context is
the repository root, so run this **from the repository root**:

```bash
docker compose -f docker/docker-compose.yml up --build
```

Results appear in `./output` on the host and `./Query.txt` is consumed in place,
so re-running resumes where the previous run stopped. Tune the run through the
`environment:` block in `docker/docker-compose.yml`. More commands are in
[docker/Docker-Commands.txt](docker/Docker-Commands.txt).

## Optional SFTP upload

Off by default. Copy `docker/.env.example`, fill it in, and set `GMAPS_UPLOAD=1`
(or pass `--upload`):

```bash
pip install -r requirements-upload.txt
```

Host keys are verified against a `known_hosts` file, and an unknown host is
refused rather than trusted on first sight. Add the server's key only after
confirming its fingerprint through a channel you trust:

```bash
ssh-keyscan -H sftp.example.com >> ~/.ssh/known_hosts
```

Credentials are read from the environment. Do not put them in source — the
previous version kept the host, username and password as module constants in
`utils.py`, which is how real credentials end up in git history.

## Troubleshooting

**A consent or CAPTCHA page appears.** The scraper detects this and stops with
an explanation rather than writing a run's worth of empty files. Run without
`--headless`, clear the challenge by hand, then continue.

**No listings found for a term.** Check `scraper.log`. The term stays in
`Query.txt`, so it will be retried on the next run.

**Selectors stopped matching.** Google generates its class names and rotates
them. Extraction prefers stable attributes (`data-item-id`, `aria-label`,
`role`) and falls back to the older class-based selectors, so a rotation
usually degrades one column rather than emptying the sheet. The selector lists
are at the top of `scraper.py`.

**`output/` already contains other results.** Merging reads every workbook in
the output directory except its own `merged_output.xlsx`. Point `--output-dir`
somewhere fresh, or clear the folder, if you do not want older files included.

## Email scraper

`scrape-emails/` holds two companion tools that work on a CSV containing a
`Website` column, such as one exported from the output above.

```bash
cd scrape-emails
pip install -r requirements.txt
playwright install chromium

python email-scrapper.py results.csv -c 8   # 8 sites at a time
python "email validator.py" results_email_scrapped.csv out.xlsx
```

Both accept file paths as arguments and fall back to a file dialog when run
without them.
