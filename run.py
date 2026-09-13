"""Command-line entry point for the Google Maps scraper.

Usage:
    python run.py                              # prompt for input source
    python run.py --search "Plumbers in London"
    python run.py --query-file Query.txt --non-interactive
    python run.py --headless --total 50

Note: scraping Google Maps is contrary to Google's Terms of Service. For
production or commercial use, prefer the official Places API.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

from config import DEFAULT_MAX_SCROLL_ATTEMPTS, DEFAULT_TOTAL, Settings
from scraper import BlockedError, scrape_search_term
from utils import (
    QueryFileError,
    log_failed_urls,
    merge_excel_files,
    read_search_terms,
    remove_search_term,
    save_listings,
)

logger = logging.getLogger("gmaps")

MAPS_URL = "https://www.google.com/maps?hl=en"
BLOCKED_RESOURCES = {"image", "media", "font"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scrape business listings from Google Maps into Excel files."
    )
    parser.add_argument(
        "--search",
        action="append",
        metavar="TERM",
        help="Search term to scrape. Repeatable. Skips the query file.",
    )
    parser.add_argument(
        "--query-file",
        type=Path,
        default=None,
        help="File of search terms, one per line (default: Query.txt).",
    )
    parser.add_argument(
        "--total",
        type=int,
        default=None,
        help=f"Listings to target per search term (default: {DEFAULT_TOTAL}).",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=None, help="Directory for Excel output."
    )
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run the browser without a visible window.",
    )
    parser.add_argument(
        "--channel",
        default=None,
        help="Browser channel, e.g. 'chrome'. Omit to use Playwright's Chromium.",
    )
    parser.add_argument(
        "--executable-path",
        default=None,
        help="Explicit browser binary path. Rarely needed.",
    )
    parser.add_argument(
        "--max-scroll-attempts",
        type=int,
        default=None,
        help=(
            "Give up scrolling after this many attempts with no new results "
            f"(default: {DEFAULT_MAX_SCROLL_ATTEMPTS})."
        ),
    )
    parser.add_argument(
        "--merge",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Merge per-term workbooks into merged_output.xlsx when done.",
    )
    parser.add_argument(
        "--keep-queries",
        action="store_true",
        help="Do not remove successfully scraped terms from the query file.",
    )
    parser.add_argument(
        "--upload",
        action="store_true",
        help="Upload results over SFTP (see uploader.py for configuration).",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Never prompt; read terms from the query file.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Console log verbosity.",
    )
    return parser


def configure_logging(level: str) -> None:
    """Log to the console and to scraper.log.

    Previously only ``run.py`` logged, and only at ERROR, while every other
    module used ``print`` -- so the log file stayed nearly empty during failures.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level))
    console.setFormatter(logging.Formatter("%(levelname)-8s %(message)s"))
    root.addHandler(console)

    try:
        file_handler = logging.FileHandler("scraper.log", encoding="utf-8")
    except OSError:
        return
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    root.addHandler(file_handler)


def settings_from_args(args: argparse.Namespace) -> Settings:
    """Environment provides defaults; explicit CLI flags win."""
    settings = Settings.from_env()
    if args.total is not None:
        settings.total = args.total
    if args.output_dir is not None:
        settings.output_dir = args.output_dir
    if args.query_file is not None:
        settings.query_file = args.query_file
    if args.headless is not None:
        settings.headless = args.headless
    if args.channel is not None:
        settings.channel = args.channel
    if args.executable_path is not None:
        settings.executable_path = args.executable_path
    if args.max_scroll_attempts is not None:
        settings.max_scroll_attempts = args.max_scroll_attempts
    if args.merge is not None:
        settings.merge = args.merge
    if args.keep_queries:
        settings.consume_queries = False
    if args.upload:
        settings.upload = True
    if settings.total < 1:
        raise SystemExit("--total must be at least 1.")
    return settings


def resolve_search_terms(args: argparse.Namespace, settings: Settings) -> "list[str]":
    """Decide what to scrape: explicit terms, the query file, or a prompt."""
    if args.search:
        terms = [term.strip() for term in args.search if term.strip()]
        if not terms:
            raise SystemExit("No usable search terms were provided.")
        return terms

    interactive = not args.non_interactive and sys.stdin is not None and sys.stdin.isatty()
    if interactive:
        choice = input(
            "Enter a search term manually (1) or use "
            f"{settings.query_file} (2)? [2] "
        ).strip()
        if choice == "1":
            term = input("Search term: ").strip()
            if not term:
                raise SystemExit("No search term entered.")
            return [term]

    return read_search_terms(settings.query_file)


async def _prepare_page(browser, settings: Settings):
    page = await browser.new_page(viewport={"width": 1280, "height": 900})
    if settings.block_images:
        # Cuts page weight substantially; listing data is all text.
        async def _route(route):
            if route.request.resource_type in BLOCKED_RESOURCES:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", _route)
    return page


async def scrape_all(terms: "list[str]", settings: Settings) -> int:
    """Scrape every term. Returns the number that failed."""
    failures = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**settings.launch_kwargs())
        try:
            page = await _prepare_page(browser, settings)
            await page.goto(MAPS_URL, timeout=settings.nav_timeout_ms)
            await page.wait_for_selector(
                '//input[@id="searchboxinput"]', timeout=settings.nav_timeout_ms
            )

            for term in terms:
                logger.info("------ %s ------", term)
                try:
                    outcome = await scrape_search_term(page, term, settings)
                except BlockedError as exc:
                    # Affects every subsequent term, so stop rather than
                    # producing a run's worth of empty files.
                    logger.error("%s", exc)
                    return failures + (len(terms) - terms.index(term))
                except Exception as exc:
                    logger.exception("Error processing %r: %s", term, exc)
                    failures += 1
                    continue

                log_failed_urls(outcome.failed_urls)

                path = save_listings(outcome.listings, term, settings)
                if path is None:
                    # Nothing was written, so the term stays in the queue for
                    # a later retry instead of being silently discarded.
                    logger.error("Keeping %r in the queue for a retry.", term)
                    failures += 1
                    continue

                if settings.upload:
                    from uploader import upload_file

                    upload_file(path)

                if settings.consume_queries:
                    remove_search_term(settings.query_file, term)

                logger.info(
                    "%r: saved %d listings (%d failed)",
                    term,
                    len(outcome.listings),
                    len(outcome.failed_urls),
                )
        finally:
            await browser.close()

    return failures


async def main_async(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.log_level)

    try:
        settings = settings_from_args(args)
        terms = resolve_search_terms(args, settings)
    except QueryFileError as exc:
        logger.error("%s", exc)
        return 2

    if not terms:
        logger.error("No search terms to process.")
        return 2

    # Ad-hoc terms from --search must never mutate the query file.
    if args.search:
        settings.consume_queries = False

    logger.info("Scraping %d search term(s), up to %d listings each", len(terms), settings.total)
    started = time.time()
    failures = await scrape_all(terms, settings)
    logger.info("Finished in %.2f minutes", (time.time() - started) / 60)

    if settings.merge:
        merged = merge_excel_files(settings)
        if settings.upload and merged is not None:
            from uploader import upload_file

            upload_file(merged)

    if failures:
        logger.error("%d search term(s) failed. See scraper.log for details.", failures)
        return 1
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.warning("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
