"""Appendix locator: finds the page ranges of Appendices A, B, and C.

The locator does NOT rely on fixed page numbers. It scans every page for
heading-like text spans that begin with "APPENDIX" followed by a single
uppercase letter, and uses font metadata (large bold text) to distinguish
real headings from TOC entries and body-text mentions.

The algorithm:
  1. Walk every page, every text span.
  2. Detect spans whose text starts with the regex ``^APPENDIX\\s+([A-Z])\\b``.
  3. Require the span to be BOTH bold AND have size >= HEADING_MIN_SIZE.
     (On the NFRC 101 PDF this means 14.52pt+ vs the 12pt TOC entries.)
  4. Record the *first* qualifying page for each appendix letter.
  5. Compute end pages: appendix ends where the next appendix begins, or at
     the end of the document.

This tolerates future page count changes, additional appendices, and minor
font variations because the heading predicate is based on relative prominence
rather than absolute coordinates.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

# Minimum font size (in points) for a span to count as a real heading.
# NFRC 101 actual appendix headings are 14.52pt (lowercase) / 18pt (caps);
# TOC entries are 12pt. We pick 13.5pt as a safe midpoint with margin.
HEADING_MIN_SIZE: float = 13.5

# Predicate regex. We accept the bare "APPENDIX A" as well as the longer
# "APPENDIX A BASIC SET OF..." form. Case-insensitive on the literal but
# the single-letter must be A-Z.
_HEADING_RE = re.compile(r"^\s*APPENDIX\s+([A-Z])\b", re.IGNORECASE)

# Targets we care about for the primary pipeline.
TARGET_APPENDICES: tuple[str, ...] = ("A", "B", "C")


@dataclass(frozen=True)
class AppendixRange:
    """Page range (1-indexed, inclusive) for an appendix section."""

    letter: str
    start_page: int  # 1-indexed
    end_page: int    # 1-indexed, inclusive

    @property
    def page_count(self) -> int:
        return self.end_page - self.start_page + 1

    def to_dict(self) -> dict[str, object]:
        return {
            "letter": self.letter,
            "start_page": self.start_page,
            "end_page": self.end_page,
            "page_count": self.page_count,
        }


@dataclass(frozen=True)
class AppendixMap:
    """Mapping of appendix letter -> AppendixRange for all detected appendices."""

    ranges: dict[str, AppendixRange]

    def get(self, letter: str) -> AppendixRange | None:
        return self.ranges.get(letter.upper())

    def targets(self) -> dict[str, AppendixRange]:
        """Return only the target appendices (A, B, C)."""
        return {l: self.ranges[l] for l in TARGET_APPENDICES if l in self.ranges}

    def missing_targets(self) -> list[str]:
        return [l for l in TARGET_APPENDICES if l not in self.ranges]


class AppendixLocatorError(RuntimeError):
    """Raised when one or more target appendices cannot be located."""


def _is_bold(font_name: str) -> bool:
    """Heuristic: does the font name indicate bold weight?"""
    n = font_name.lower()
    return "bold" in n or "black" in n or "heavy" in n


def _page_heading_letters(page: "fitz.Page") -> set[str]:
    """Return the set of appendix letters whose heading appears on this page.

    A span qualifies if:
      * Its text matches the APPENDIX-letter regex.
      * Its font is bold.
      * Its size is >= HEADING_MIN_SIZE.
    """
    letters: set[str] = set()
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            # Reconstruct the line text by concatenating span texts in order.
            spans = line.get("spans", [])
            line_text = "".join(s.get("text", "") for s in spans)
            m = _HEADING_RE.match(line_text)
            if not m:
                continue
            letter = m.group(1).upper()
            # Check that AT LEAST ONE span in the line is bold and large enough.
            # (NFRC 101 mixes 18pt caps and 14.52pt lowercase spans in the same
            # heading line; either qualifies.)
            qualifies = False
            for s in spans:
                size = float(s.get("size", 0.0))
                font = str(s.get("font", ""))
                txt = s.get("text", "")
                # The span must contribute to the "APPENDIX X" prefix.
                if not txt:
                    continue
                if _is_bold(font) and size >= HEADING_MIN_SIZE:
                    qualifies = True
                    break
            if qualifies:
                letters.add(letter)
    return letters


def locate_appendices(pdf_path: Path) -> AppendixMap:
    """Locate all appendix headings in the PDF and return their page ranges.

    The first qualifying page for each letter is its start_page. End pages
    are inferred from the next appendix's start, or the end of the document
    for the last appendix.

    Raises:
        AppendixLocatorError: if NO appendices at all are detected (suggests
            a fundamentally different PDF structure or a corrupt file).
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise AppendixLocatorError(f"PDF not found: {pdf_path}")

    doc = fitz.open(str(pdf_path))
    total_pages = doc.page_count
    logger.info("Scanning %d pages for appendix headings in %s",
                total_pages, pdf_path.name)

    # Letter -> first page (1-indexed) where its heading appears.
    first_page: dict[str, int] = {}
    for i, page in enumerate(doc, start=1):
        letters = _page_heading_letters(page)
        for letter in letters:
            if letter not in first_page:
                first_page[letter] = i
                logger.info(
                    "Detected Appendix %s heading on page %d", letter, i
                )

    if not first_page:
        raise AppendixLocatorError(
            "No appendix headings detected in PDF. Heading predicate may need "
            "adjustment, or the PDF is not NFRC 101."
        )

    # Sort letters by their first-occurrence page to compute end pages.
    sorted_letters = sorted(first_page.keys(), key=lambda l: first_page[l])
    ranges: dict[str, AppendixRange] = {}
    for idx, letter in enumerate(sorted_letters):
        start = first_page[letter]
        if idx + 1 < len(sorted_letters):
            end = first_page[sorted_letters[idx + 1]] - 1
        else:
            end = total_pages
        # Clamp in case of overlapping detections.
        if end < start:
            end = start
        ranges[letter] = AppendixRange(letter=letter, start_page=start, end_page=end)
        logger.info(
            "Appendix %s: pages %d-%d (%d pages)",
            letter, start, end, ranges[letter].page_count,
        )

    doc.close()
    return AppendixMap(ranges=ranges)


def require_targets(pdf_path: Path) -> dict[str, AppendixRange]:
    """Locate appendices and raise if A, B, or C is missing.

    Returns the dict of target ranges {letter: AppendixRange}.
    """
    amap = locate_appendices(pdf_path)
    missing = amap.missing_targets()
    if missing:
        raise AppendixLocatorError(
            f"Required appendices not found in PDF: {', '.join(missing)}. "
            f"Detected appendices: {sorted(amap.ranges.keys())}"
        )
    return amap.targets()
