"""Runtime configuration.

Everything that used to be edited in place in the source -- the result target,
headless mode, the Chrome executable path -- is a setting here, resolved from
CLI flags with environment variables as the fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TOTAL = 25
DEFAULT_MAX_SCROLL_ATTEMPTS = 10
MERGED_FILENAME = "merged_output.xlsx"


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def env_str(name: str, default: "str | None" = None) -> "str | None":
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip()


@dataclass
class Settings:
    """Resolved configuration for a scraping run."""

    total: int = DEFAULT_TOTAL
    headless: bool = False
    output_dir: Path = Path("output")
    query_file: Path = Path("Query.txt")

    # Browser selection.  Both default to None, which uses the Chromium that
    # `playwright install` downloaded -- no machine-specific path required.
    channel: "str | None" = None
    executable_path: "str | None" = None

    max_scroll_attempts: int = DEFAULT_MAX_SCROLL_ATTEMPTS
    nav_retries: int = 2
    nav_timeout_ms: int = 60_000
    detail_settle_ms: int = 1_500
    block_images: bool = True

    # When false, a successfully scraped term stays in Query.txt.
    consume_queries: bool = True
    merge: bool = True
    upload: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            total=env_int("GMAPS_TOTAL", DEFAULT_TOTAL),
            headless=env_bool("GMAPS_HEADLESS", False),
            output_dir=Path(env_str("GMAPS_OUTPUT_DIR", "output")),
            query_file=Path(env_str("GMAPS_QUERY_FILE", "Query.txt")),
            channel=env_str("GMAPS_BROWSER_CHANNEL"),
            executable_path=env_str("GMAPS_CHROME_PATH"),
            max_scroll_attempts=env_int(
                "GMAPS_MAX_SCROLL_ATTEMPTS", DEFAULT_MAX_SCROLL_ATTEMPTS
            ),
            nav_retries=env_int("GMAPS_NAV_RETRIES", 2),
            nav_timeout_ms=env_int("GMAPS_NAV_TIMEOUT_MS", 60_000),
            block_images=env_bool("GMAPS_BLOCK_IMAGES", True),
            consume_queries=env_bool("GMAPS_CONSUME_QUERIES", True),
            merge=env_bool("GMAPS_MERGE", True),
            upload=env_bool("GMAPS_UPLOAD", False),
        )

    def launch_kwargs(self) -> dict:
        """Build ``chromium.launch()`` arguments.

        Neither ``channel`` nor ``executable_path`` is set by default, so
        Playwright uses its own bundled browser.  That removes the hardcoded
        ``C:\\Program Files\\...`` path the old code depended on, which also
        happened to contain invalid string escapes.
        """
        args = ["--lang=en-US", "--disable-dev-shm-usage"]
        if self.headless:
            # Needed when running as root inside a container.
            args.append("--no-sandbox")
        kwargs: dict = {"headless": self.headless, "args": args}
        if self.channel:
            kwargs["channel"] = self.channel
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        return kwargs
