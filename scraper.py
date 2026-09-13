"""Google Maps scraping routines.

Two phases per search term:

1. ``collect_listing_urls`` scrolls the results feed and gathers place URLs.
2. ``scrape_listing_details`` visits each URL and builds a :class:`Listing`.

Both reuse the page supplied by the caller -- the old code opened a second
Playwright instance and a second browser halfway through the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from tqdm import tqdm

from config import Settings
from models import Listing, parse_rating, parse_reviews_count
from playwright_helpers import (
    get_element_attribute,
    get_element_text,
    get_first_text,
    looks_blocked,
    navigate,
)

logger = logging.getLogger(__name__)

PLACE_LINK = '//a[contains(@href, "/maps/place")]'
RESULTS_FEED = '//div[@role="feed"]'
SEARCH_BOX = '//input[@id="searchboxinput"]'
# Matched on text rather than the generated class name the old code relied on.
END_OF_LIST = '//span[contains(text(), "end of the list")]'

# Each field tries stable attributes first (data-item-id, aria-label, role),
# then falls back to the generated class names used by earlier versions.
NAME_SELECTORS = (
    '//div[@role="main"]//h1',
    '//div[@style="padding-bottom: 4px;"]//h1',
    "//h1",
)
CATEGORY_SELECTORS = (
    '//button[contains(@jsaction, "category")]',
    '//div[@class="LBgpqf"]//button[contains(@class, "DkEaL")]',
)
PLUS_CODE_SELECTORS = (
    '//button[@data-item-id="oloc"]//div[contains(@class, "fontBodyMedium")]',
    '//button[contains(@aria-label, "Plus code")]//div[contains(@class, "fontBodyMedium")]',
)
ADDRESS_SELECTORS = (
    '//button[@data-item-id="address"]//div[contains(@class, "fontBodyMedium")]',
    '//button[contains(@aria-label, "Address")]//div[contains(@class, "fontBodyMedium")]',
)
PHONE_SELECTORS = (
    '//button[contains(@data-item-id, "phone:tel:")]//div[contains(@class, "fontBodyMedium")]',
    '//button[contains(@aria-label, "Phone")]//div[contains(@class, "fontBodyMedium")]',
)
WEBSITE_SELECTORS = (
    '//a[@data-item-id="authority"]',
    '//a[@data-value="Open website"]',
)
RATING_SELECTORS = (
    '//div[@role="main"]//span[@role="img"][contains(@aria-label, "star")]',
    '//div[@style="padding-bottom: 4px;"]//div[contains(@jslog, "mutable:true;")]/span[1]/span[1]',
)
REVIEWS_SELECTORS = (
    '//div[@role="main"]//button[contains(@aria-label, "review")]',
    '//div[@style="padding-bottom: 4px;"]//div[contains(@jslog, "mutable:true;")]/span[2]',
)


class BlockedError(RuntimeError):
    """Raised when Google serves a consent or CAPTCHA page instead of results."""


@dataclass
class ScrapeOutcome:
    """What a single search term produced."""

    term: str
    listings: "list[Listing]" = field(default_factory=list)
    failed_urls: "list[str]" = field(default_factory=list)
    urls_found: int = 0


async def open_search(page, term: str, settings: Settings) -> bool:
    """Run a search and wait for the results feed.

    Returns False when the search produced no usable results panel, so the
    caller can record a failure rather than saving an empty file.
    """
    await page.locator(SEARCH_BOX).first.fill(term)
    await page.keyboard.press("Enter")

    try:
        await page.wait_for_selector(PLACE_LINK, timeout=settings.nav_timeout_ms // 2)
        return True
    except Exception:
        # A narrow query can land straight on a single place page, where there
        # is no results feed to wait for.
        if "/maps/place" in page.url:
            logger.info("Search resolved to a single place page")
            return True
        # Distinguish "Google blocked us" from "this query genuinely has no
        # results" -- the old code could not tell these apart.
        if await looks_blocked(page):
            raise BlockedError(
                "Google served a consent/CAPTCHA page. Run without --headless "
                "and complete the challenge, or slow the run down."
            )
        logger.warning("No listings appeared for %r", term)
        return False


async def _scroll_feed(page) -> None:
    """Scroll the results list.

    ``mouse.wheel`` alone scrolls whatever is under the cursor, which is often
    the map rather than the list.  Scrolling the feed element directly is what
    actually loads more results.
    """
    feed = page.locator(RESULTS_FEED).first
    if await feed.count() > 0:
        try:
            await feed.evaluate("el => el.scrollBy(0, el.scrollHeight)")
            return
        except Exception as exc:
            logger.debug("Feed scroll failed, falling back to wheel: %s", exc)
    try:
        await page.mouse.wheel(0, 10_000)
    except Exception as exc:
        logger.debug("Wheel scroll failed: %s", exc)


async def collect_listing_urls(page, settings: Settings) -> "list[str]":
    """Scroll until ``settings.total`` place URLs are visible, then return them.

    The loop is bounded by ``settings.max_scroll_attempts``.  The previous root
    version had no such bound: when the count stopped rising and the
    end-of-list marker did not match, it scrolled and clicked forever.
    """
    seen: "dict[str, None]" = {}

    async def harvest() -> int:
        for handle in await page.locator(PLACE_LINK).all():
            try:
                href = await handle.get_attribute("href")
            except Exception:
                continue
            if href and "/maps/place" in href:
                # dict preserves insertion order and drops repeats, including
                # the link for the place panel that is currently open.
                seen.setdefault(href, None)
        return len(seen)

    count = await harvest()
    logger.info("Found %d listings", count)

    if count == 0 and "/maps/place" in page.url:
        return [page.url]

    stalled = 0
    while count < settings.total and stalled < settings.max_scroll_attempts:
        await _scroll_feed(page)
        await page.wait_for_timeout(1_200)
        new_count = await harvest()

        if new_count > count:
            logger.info("Scrolled to %d listings", new_count)
            stalled = 0
        else:
            stalled += 1
            if await page.locator(END_OF_LIST).count() > 0:
                logger.info("Reached the end of the results list")
                break
            logger.debug(
                "No new listings (attempt %d/%d)", stalled, settings.max_scroll_attempts
            )
        count = new_count

    if stalled >= settings.max_scroll_attempts:
        logger.info(
            "Stopped after %d attempts with no new listings", settings.max_scroll_attempts
        )

    return list(seen)[: settings.total]


async def _extract_one(page, url: str, settings: Settings) -> Listing:
    """Build a Listing from an already-loaded place page."""
    await page.wait_for_timeout(settings.detail_settle_ms)

    listing = Listing()
    # page.url is the resolved URL, which carries the @lat,lon segment.
    listing.google_link = page.url
    listing.set_coordinates_from_url(page.url)

    listing.name = await get_first_text(page, NAME_SELECTORS)
    listing.category = await get_first_text(page, CATEGORY_SELECTORS)
    listing.plus_code = await get_first_text(page, PLUS_CODE_SELECTORS)
    listing.address = await get_first_text(page, ADDRESS_SELECTORS)
    listing.phone = await get_first_text(page, PHONE_SELECTORS)

    website = ""
    for selector in WEBSITE_SELECTORS:
        website = await get_element_attribute(page, selector, "href")
        if website:
            break
    listing.website = website

    rating_text = ""
    for selector in RATING_SELECTORS:
        rating_text = await get_element_attribute(page, selector, "aria-label")
        if not rating_text:
            rating_text = await get_element_text(page, selector)
        if rating_text:
            break
    listing.rating = parse_rating(rating_text)

    reviews_text = ""
    for selector in REVIEWS_SELECTORS:
        reviews_text = await get_element_attribute(page, selector, "aria-label")
        if not reviews_text:
            reviews_text = await get_element_text(page, selector)
        if reviews_text:
            break
    listing.reviews_count = parse_reviews_count(reviews_text)

    return listing


async def scrape_listing_details(
    page, urls: "list[str]", settings: Settings
) -> "tuple[list[Listing], list[str]]":
    """Visit each URL and collect listings, isolating per-listing failures.

    The old version wrapped the whole loop in one ``try``, so a single slow
    page aborted every remaining listing in the batch.  Here each URL is
    independent and failures are collected for reporting.
    """
    listings: "list[Listing]" = []
    failed: "list[str]" = []

    for url in tqdm(urls, desc="listings", unit="place"):
        try:
            if not await navigate(
                page, url, retries=settings.nav_retries, timeout=settings.nav_timeout_ms
            ):
                failed.append(url)
                continue

            listing = await _extract_one(page, url, settings)
            if listing.is_empty:
                logger.warning("No data extracted from %s", url)
                failed.append(url)
                continue
            listings.append(listing)
        except Exception as exc:
            # One bad listing must not end the batch.
            logger.warning("Failed to extract %s: %s", url, exc)
            failed.append(url)

    return listings, failed


async def scrape_search_term(page, term: str, settings: Settings) -> ScrapeOutcome:
    """Scrape one search term end to end."""
    if not await open_search(page, term, settings):
        return ScrapeOutcome(term=term)

    urls = await collect_listing_urls(page, settings)
    logger.info("Processing %d listings for %r", len(urls), term)
    if not urls:
        return ScrapeOutcome(term=term)

    listings, failed = await scrape_listing_details(page, urls, settings)
    return ScrapeOutcome(
        term=term, listings=listings, failed_urls=failed, urls_found=len(urls)
    )
