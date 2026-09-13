"""Search-queue handling and Excel output."""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from config import MERGED_FILENAME, Settings
from models import HEADERS, Listing

logger = logging.getLogger(__name__)

# Characters Windows forbids in filenames, plus path separators.
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MAX_COLUMN_WIDTH = 60


class QueryFileError(Exception):
    """Raised when the query file is missing or unusable."""


def sanitize_filename(name: str) -> str:
    """Turn a search term into a safe filename stem.

    The old code only replaced spaces, so a term containing ``/`` or ``:``
    produced an invalid path on Windows -- and a term from an untrusted query
    file could escape the output directory entirely.
    """
    stem = _UNSAFE_FILENAME.sub("", name).replace(" ", "_").strip("._").lower()
    # Collapse runs of separators left behind by stripped characters.
    stem = re.sub(r"_{2,}", "_", stem)
    return stem[:120] or "results"


def read_search_terms(query_file: Path) -> "list[str]":
    """Read search terms from a file, skipping blanks and duplicates.

    Raises instead of calling ``sys.exit()`` so callers stay testable.
    """
    if not query_file.exists():
        raise QueryFileError(f"{query_file} not found.")

    # utf-8-sig strips a byte-order mark if the file was saved from Notepad.
    text = query_file.read_text(encoding="utf-8-sig")
    terms: "list[str]" = []
    for line in text.splitlines():
        term = line.strip()
        # Blank lines used to become empty searches.
        if not term or term.startswith("#"):
            continue
        if term not in terms:
            terms.append(term)
    return terms


def remove_search_term(query_file: Path, term: str) -> bool:
    """Drop ``term`` from the query file. Returns True if a line was removed.

    Kept separate from saving so a term is only ever consumed after its results
    have actually been written.
    """
    if not query_file.exists():
        logger.warning("%s not found; nothing to update.", query_file)
        return False

    text = query_file.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    kept = [line for line in lines if line.strip() != term]
    if len(kept) == len(lines):
        logger.warning("%r was not found in %s", term, query_file)
        return False

    query_file.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    logger.info("Removed %r from %s", term, query_file)
    return True


def to_dataframe(listings: "list[Listing]") -> pd.DataFrame:
    """Build a DataFrame with the canonical column order.

    Every row comes from one Listing, so columns cannot be misaligned relative
    to each other regardless of which fields failed to extract.
    """
    frame = pd.DataFrame([listing.to_row() for listing in listings], columns=HEADERS)
    return frame.drop_duplicates(subset=["Google Link"], keep="first")


def save_listings(listings: "list[Listing]", term: str, settings: Settings) -> "Path | None":
    """Write listings to ``<output_dir>/<term>.xlsx``.

    Returns the path written, or None when there was nothing to write.  Refusing
    to write an empty workbook is what stops a failed scrape from looking like a
    successful one.
    """
    if not listings:
        logger.error("No listings scraped for %r; not writing a file.", term)
        return None

    # Drop records where nothing identifying was captured. Without this, a run
    # where every selector missed would still produce a full-looking workbook
    # of blank rows.
    usable = [listing for listing in listings if not listing.is_empty]
    if len(usable) != len(listings):
        logger.warning(
            "Discarded %d empty listing(s) for %r.", len(listings) - len(usable), term
        )
    if not usable:
        logger.error("Every listing for %r was empty; not writing a file.", term)
        return None

    frame = to_dataframe(usable)
    if frame.empty:
        logger.error("No rows left for %r after de-duplication; not writing a file.", term)
        return None

    settings.output_dir.mkdir(parents=True, exist_ok=True)
    path = settings.output_dir / f"{sanitize_filename(term)}.xlsx"
    frame.to_excel(path, index=False)
    autofit_columns(path)
    logger.info("Wrote %d rows to %s", len(frame), path)
    return path


def merge_excel_files(settings: Settings) -> "Path | None":
    """Combine per-term workbooks into ``merged_output.xlsx``.

    Two fixes over the previous version: the merged file is excluded from its
    own inputs (it used to re-ingest its last output on every run), and every
    frame is reindexed onto the canonical columns so a workbook with a
    different shape cannot reorder the merged result.
    """
    output_dir = settings.output_dir
    if not output_dir.exists():
        logger.warning("%s does not exist; nothing to merge.", output_dir)
        return None

    sources = sorted(
        path
        for path in output_dir.glob("*.xlsx")
        if path.name != MERGED_FILENAME and not path.name.startswith("~$")
    )
    if not sources:
        logger.warning("No per-term workbooks found in %s; nothing to merge.", output_dir)
        return None

    frames = []
    for path in sources:
        try:
            frame = pd.read_excel(path)
        except Exception as exc:
            logger.warning("Skipping unreadable workbook %s: %s", path, exc)
            continue
        if frame.empty:
            continue
        # Force a consistent schema; missing columns become NaN.
        frames.append(frame.reindex(columns=HEADERS))

    if not frames:
        logger.warning("No rows to merge in %s.", output_dir)
        return None

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=["Google Link"], keep="first")

    merged_path = output_dir / MERGED_FILENAME
    combined.to_excel(merged_path, index=False)
    autofit_columns(merged_path)
    logger.info("Merged %d rows from %d files into %s", len(combined), len(frames), merged_path)
    return merged_path


def autofit_columns(path: Path) -> None:
    """Widen columns to fit their contents, capped so URLs stay readable."""
    try:
        workbook = load_workbook(path)
        worksheet = workbook.active
        for column in worksheet.columns:
            letter = column[0].column_letter
            longest = max(
                (len(str(cell.value)) for cell in column if cell.value is not None),
                default=0,
            )
            worksheet.column_dimensions[letter].width = min(longest + 2, MAX_COLUMN_WIDTH)
        workbook.save(path)
    except Exception as exc:
        # Cosmetic only -- never fail a run because formatting did not apply.
        logger.warning("Could not adjust column widths for %s: %s", path, exc)


def log_failed_urls(urls: "list[str]", path: Path = Path("failed_links.log")) -> None:
    """Append URLs that could not be scraped so they can be retried."""
    if not urls:
        return
    try:
        with path.open("a", encoding="utf-8") as handle:
            for url in urls:
                handle.write(url + "\n")
        logger.info("Logged %d failed links to %s", len(urls), path)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)
