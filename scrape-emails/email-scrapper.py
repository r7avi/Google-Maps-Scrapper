"""Scrape email addresses from the websites listed in a CSV.

Reads a CSV containing a 'Website' column (for example a file produced by the
Google Maps scraper), visits each site plus any contact page it links to, and
writes a copy of the CSV with an 'Email' column inserted after 'Website'.

Usage:
    python email-scrapper.py                        # file picker dialog
    python email-scrapper.py results.csv            # explicit input
    python email-scrapper.py results.csv -c 8       # 8 sites at a time
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import os
import re
import sys
from urllib.parse import urljoin, urlparse, urlunparse

import pandas as pd
from lxml import html
from playwright.async_api import async_playwright

logger = logging.getLogger("email-scrapper")

# One pattern, applied once. The original defined a loose pattern and then
# re-validated each hit against a near-identical stricter one.
EMAIL_RE = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}\b"
)

# Images and tracking assets never contain the text we are looking for.
BLOCKED_RESOURCES = {"image", "media", "font"}

# Addresses that are placeholders or belong to the site's tooling, not the
# business.
IGNORED_EMAIL_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".css",
    ".js",
)
IGNORED_EMAIL_PREFIXES = ("example@", "email@", "your@", "name@", "user@")

CONTACT_HINTS = ("contact", "kontakt", "about", "impressum", "reach-us", "get-in-touch")

DEFAULT_CONCURRENCY = 5
NAV_TIMEOUT_MS = 30_000


def normalize_url(raw: str) -> "str | None":
    """Add a scheme only when one is genuinely missing.

    The original did ``'https://' + url if not url.startswith('https://')``,
    which turned ``http://example.com`` into ``https://http://example.com`` and
    silently failed on every plain-HTTP site in the input.
    """
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None

    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return urlunparse(parsed)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        # mailto:, tel:, ftp:, ... not a website.
        return None
    if parsed.scheme:
        # e.g. "https:/example.com" -- malformed, skip rather than guess.
        return None

    reparsed = urlparse(f"https://{candidate}")
    if not reparsed.netloc:
        return None
    return urlunparse(reparsed)


def clean_emails(candidates) -> "set[str]":
    """Drop obvious false positives such as filenames and placeholders."""
    keep = set()
    for email in candidates:
        lowered = email.lower()
        if lowered.endswith(IGNORED_EMAIL_SUFFIXES):
            continue
        if lowered.startswith(IGNORED_EMAIL_PREFIXES):
            continue
        keep.add(lowered)
    return keep


async def emails_on_page(page) -> "set[str]":
    """Collect addresses from the page text and from any mailto: links."""
    found: "set[str]" = set()
    try:
        content = await page.content()
    except Exception as exc:
        logger.debug("Could not read page content: %s", exc)
        return found

    try:
        text = html.fromstring(content).text_content()
        found.update(EMAIL_RE.findall(text))
    except Exception as exc:
        logger.debug("Could not parse HTML: %s", exc)

    # mailto: links are the most reliable source and are often not in the text.
    try:
        for href in await page.eval_on_selector_all(
            'a[href^="mailto:"]', "els => els.map(e => e.getAttribute('href'))"
        ):
            if href:
                found.update(EMAIL_RE.findall(href))
    except Exception as exc:
        logger.debug("Could not read mailto links: %s", exc)

    return clean_emails(found)


async def contact_page_urls(page, base_url: str, limit: int = 3) -> "list[str]":
    """Find likely contact-page links.

    Checks the href as well as the link text. The original only looked at text,
    so a link labelled "Get in touch" pointing at /contact was missed.
    """
    try:
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => [e.getAttribute('href'), (e.textContent || '').trim()])",
        )
    except Exception as exc:
        logger.debug("Could not enumerate links on %s: %s", base_url, exc)
        return []

    base_host = urlparse(base_url).netloc
    found: "list[str]" = []
    for href, text in links:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        haystack = f"{href} {text}".lower()
        if not any(hint in haystack for hint in CONTACT_HINTS):
            continue
        absolute = urljoin(base_url, href)
        # Stay on the same site.
        if urlparse(absolute).netloc != base_host:
            continue
        if absolute not in found:
            found.append(absolute)
        if len(found) >= limit:
            break
    return found


async def _goto(page, url: str) -> bool:
    try:
        # 'networkidle' stalls indefinitely on ad-heavy pages; this is both
        # faster and more reliable.
        await page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
        return True
    except Exception as exc:
        logger.warning("Could not load %s: %s", url, exc)
        return False


async def scrape_site(browser, url: str, semaphore: asyncio.Semaphore) -> str:
    """Return a comma-separated list of addresses found for one site."""
    async with semaphore:
        context = None
        page = None
        try:
            context = await browser.new_context(ignore_https_errors=True)
            page = await context.new_page()

            async def _route(route):
                if route.request.resource_type in BLOCKED_RESOURCES:
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", _route)

            if not await _goto(page, url):
                return ""

            emails = await emails_on_page(page)

            for contact_url in await contact_page_urls(page, url):
                if await _goto(page, contact_url):
                    emails.update(await emails_on_page(page))

            return ", ".join(sorted(emails))
        except Exception as exc:
            logger.warning("Error processing %s: %s", url, exc)
            return ""
        finally:
            # Guarded, because the original unconditionally closed `page` in
            # its finally block -- if new_context() raised, `page` was unbound
            # and the resulting UnboundLocalError masked the real error.
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass


def pick_input_file() -> "str | None":
    """Ask for a CSV via a file dialog, if a GUI is available."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError:
        logger.error("tkinter is unavailable. Pass the CSV path as an argument.")
        return None

    root = tk.Tk()
    root.withdraw()
    try:
        return filedialog.askopenfilename(
            title="Select CSV File", filetypes=[("CSV files", "*.csv")]
        ) or None
    finally:
        root.destroy()


def read_websites(path: str) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if "Website" not in frame.columns:
        raise ValueError("The CSV has no 'Website' column.")

    frame = frame.dropna(subset=["Website"]).copy()
    frame["Website"] = frame["Website"].map(normalize_url)
    dropped = int(frame["Website"].isna().sum())
    if dropped:
        logger.warning("Skipped %d row(s) with an unusable website value.", dropped)
    return frame.dropna(subset=["Website"]).reset_index(drop=True)


async def run(input_file: str, concurrency: int, headless: bool) -> int:
    frame = read_websites(input_file)
    if frame.empty:
        logger.error("No usable website links found.")
        return 2

    logger.info("Scraping %d site(s), %d at a time.", len(frame), concurrency)

    semaphore = asyncio.Semaphore(concurrency)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=headless)
        try:
            # Sites are processed concurrently; the original visited them one
            # at a time, which made large CSVs take hours.
            results = await asyncio.gather(
                *(scrape_site(browser, url, semaphore) for url in frame["Website"])
            )
        finally:
            await browser.close()

    frame["Email"] = results
    columns = [c for c in frame.columns if c != "Email"]
    columns.insert(columns.index("Website") + 1, "Email")
    frame = frame[columns]

    base, ext = os.path.splitext(input_file)
    output_file = f"{base}_email_scrapped{ext or '.csv'}"
    frame.to_csv(output_file, index=False, quoting=csv.QUOTE_MINIMAL)

    with_email = int((frame["Email"].astype(str).str.len() > 0).sum())
    logger.info(
        "Done. Found addresses for %d of %d sites. Saved to %s",
        with_email,
        len(frame),
        output_file,
    )
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scrape email addresses from websites listed in a CSV."
    )
    parser.add_argument("input", nargs="?", help="Input CSV (prompts if omitted).")
    parser.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Sites to process at once (default: {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window (headless by default).",
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)-8s %(message)s",
        stream=sys.stdout,
    )

    input_file = args.input or pick_input_file()
    if not input_file:
        logger.error("No input file selected.")
        return 2
    if not os.path.exists(input_file):
        logger.error("%s does not exist.", input_file)
        return 2
    if args.concurrency < 1:
        logger.error("--concurrency must be at least 1.")
        return 2

    try:
        return asyncio.run(run(input_file, args.concurrency, headless=not args.headed))
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130
    except Exception as exc:
        logger.exception("Failed: %s", exc)
        return 1


# The original called asyncio.run(main()) at module level, so merely importing
# the file started a scrape and popped up a dialog.
if __name__ == "__main__":
    raise SystemExit(main())
