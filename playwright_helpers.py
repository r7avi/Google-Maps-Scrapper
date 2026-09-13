"""Thin, defensive wrappers around Playwright locators and navigation."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Text that appears on Google's consent / anti-automation interstitials.  If one
# of these is showing, the page contains no listing data and every selector will
# come back empty -- which previously looked identical to a genuine zero-result
# search.  Detecting it lets the caller report a block instead of silently
# writing an empty spreadsheet.
_BLOCK_MARKERS = (
    "unusual traffic",
    "not a robot",
    "recaptcha",
    "before you continue",
    "verify it's you",
    "verify you're not a robot",
)


async def get_element_text(page, selector: str, timeout: int = 10_000) -> str:
    """Return the trimmed text of the first match, or ``''`` if absent.

    Uses ``.first`` so a selector matching several nodes cannot raise a
    strict-mode violation.
    """
    try:
        element = page.locator(selector).first
        if await element.count() == 0:
            return ""
        text = await element.inner_text(timeout=timeout)
        return (text or "").strip()
    except Exception as exc:
        logger.debug("No text for selector %s: %s", selector, exc)
        return ""


async def get_element_attribute(
    page, selector: str, attribute: str, timeout: int = 10_000
) -> str:
    """Return an attribute of the first match, or ``''`` if absent."""
    try:
        element = page.locator(selector).first
        if await element.count() == 0:
            return ""
        value = await element.get_attribute(attribute, timeout=timeout)
        return (value or "").strip()
    except Exception as exc:
        logger.debug("No attribute %s for selector %s: %s", attribute, selector, exc)
        return ""


async def get_first_text(page, selectors, timeout: int = 5_000) -> str:
    """Try several selectors in order and return the first non-empty result.

    Google Maps markup is generated, so class-name based selectors break without
    notice.  Providing a fallback chain means one rotated class name degrades a
    single field instead of emptying the column.
    """
    for selector in selectors:
        text = await get_element_text(page, selector, timeout=timeout)
        if text:
            return text
    return ""


async def navigate(page, url: str, retries: int = 2, timeout: int = 60_000) -> bool:
    """Navigate to ``url``, retrying transient failures.

    Returns True on success and False once the attempts are exhausted.  The
    previous version could fall out of its loop and return ``None`` when
    ``retries`` was 0, which the caller then treated as success.
    """
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        try:
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            return True
        except Exception as exc:
            logger.warning(
                "Navigation attempt %d/%d failed for %s: %s", attempt, attempts, url, exc
            )
            # A failed navigation can leave the tab on an interstitial error
            # page, which then interrupts the very next goto. Settling after
            # every failure -- including the last -- keeps one bad URL from
            # taking the following one down with it.
            await _settle(page, attempt)
    return False


async def _settle(page, attempt: int) -> None:
    """Pause between navigation attempts, backing off as they accumulate."""
    try:
        await page.wait_for_timeout(min(500 * attempt, 3_000))
    except Exception:
        pass


async def looks_blocked(page) -> bool:
    """True when the current page looks like a consent or CAPTCHA wall."""
    try:
        body = (await page.inner_text("body", timeout=5_000)).lower()
    except Exception:
        return False
    return any(marker in body for marker in _BLOCK_MARKERS)
