"""Table extractor: pulls tables from a page range and reconstructs full records.

NFRC 101 appendix tables have specific quirks:
  * Multi-page tables repeat the header row on every page.
  * Some pages have multiple small tables (e.g. Appendix A's gas table on
    page 51 has a conductivity/viscosity table AND a separate specific-heat
    table that share the 'Gas' column).
  * Rows can wrap (a single material's name cell may be split across two
    visual rows).
  * Section headers appear as single-cell-populated rows
    (e.g. 'Elastomers', 'Polymers').
  * Some cells contain 'See Appendix A' instead of numeric values.

Strategy:
  1. Use pdfplumber's `find_tables()` on every page in the appendix range.
  2. Filter out spurious tables (very small tables, tables with no header-like
     first row).
  3. For tables with the SAME column count and a matching header, merge rows
     across pages, dropping repeated header rows.
  4. Reconstruct wrapped rows: if a row has a populated first cell but the
     rest are empty AND the next row has an empty first cell, the second row
     is a continuation of the first.
  5. Return a single normalized table per appendix with a stable column
     schema.

This module is intentionally generic about *what* the columns mean — that
interpretation lives in `material_parser.py`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import pdfplumber

from .appendix_locator import AppendixRange

logger = logging.getLogger(__name__)


# A "cell" is just a string after normalization. None becomes "".
Cell = str
# A "row" is a list of cells.
Row = list[Cell]
# A "table" is a list of rows.
Table = list[Row]


@dataclass(frozen=True)
class ExtractedTable:
    """One merged, normalized table for an appendix section."""

    appendix_letter: str
    header: Row                 # Normalized header (lowercased identifiers).
    raw_header: Row             # Original header text (for debugging).
    rows: Row.__class__         # type: ignore[type-arg]  # data rows (header stripped)
    source_pages: tuple[int, ...]  # 1-indexed pages that contributed rows.
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "appendix_letter": self.appendix_letter,
            "header": self.header,
            "raw_header": self.raw_header,
            "row_count": len(self.rows),
            "source_pages": list(self.source_pages),
            "notes": self.notes,
            "rows": self.rows,
        }


class TableExtractionError(RuntimeError):
    """Raised when no usable tables can be extracted from a page range."""


# ---------------------------------------------------------------------------
# Cell / row normalization helpers
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_NONPRINT_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def normalize_cell(value: Any) -> Cell:
    """Normalize a single cell value to a clean string.

    * None -> ""
    * Newlines within a cell become " / " (preserves multi-line cell content
      such as 'CO / 2' which the PDF renders as 'CO2' with a subscript).
    * Whitespace runs collapse to single spaces.
    * Control characters stripped.
    * Surrounding whitespace trimmed.
    """
    if value is None:
        return ""
    s = str(value)
    s = _NONPRINT_RE.sub("", s)
    s = s.replace("\r", " ").replace("\n", " / ").replace("\t", " ")
    s = _WS_RE.sub(" ", s).strip()
    return s


def normalize_row(row: Iterable[Any]) -> Row:
    """Normalize every cell in a row."""
    return [normalize_cell(c) for c in row]


def is_blank_row(row: Row) -> bool:
    """True iff every cell is empty (or only whitespace, already normalized)."""
    return all(c == "" for c in row)


def is_section_header_row(row: Row) -> bool:
    """Heuristic: a row where ONLY the first cell is non-empty.

    NFRC uses these as category headers (e.g. 'Elastomers', 'Polymers').
    The parser may either skip them or use them to enrich subsequent rows.
    """
    if not row:
        return False
    first = row[0]
    rest = row[1:]
    return first != "" and all(c == "" for c in rest)


# ---------------------------------------------------------------------------
# Header detection
# ---------------------------------------------------------------------------


def _looks_like_header(row: Row, expected_keywords: tuple[str, ...]) -> bool:
    """Heuristic: does this row look like a table header?

    Matches if ANY expected keyword appears (case-insensitive) in ANY cell
    of the row. Used for *dropping repeated headers* mid-stream where we
    already know the table is a real one.
    """
    if not row:
        return False
    blob = " | ".join(row).lower()
    return any(kw in blob for kw in expected_keywords)


def _is_strict_header_match(row: Row, required_keywords: tuple[str, ...]) -> bool:
    """Stricter: ALL `required_keywords` must appear in the row.

    Used to distinguish real appendix tables from unrelated tables that share
    a keyword. For example, Appendix A's gas-data table on page 51 contains
    'conductivity' but NOT 'emissivity' or 'name', so it is correctly rejected.
    """
    if not row:
        return False
    blob = " | ".join(row).lower()
    return all(kw in blob for kw in required_keywords)


# Header keyword anchors per appendix. We accept several variants to be
# robust to PDF revision changes.
_HEADER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "A": ("name", "conductivity", "emissivity"),
    "B": ("name", "conductivity", "emissivity"),
    "C": ("participant", "product", "conductivity", "emissivity"),
}

# Strict (AND-match) required keywords per appendix. A table is only kept if
# its header row contains ALL of these. This excludes unrelated tables that
# happen to share a keyword (e.g. Appendix A's gas-coefficient table on
# page 51 has 'conductivity' but lacks 'name' and 'emissivity').
_STRICT_HEADER_REQUIRED: dict[str, tuple[str, ...]] = {
    "A": ("name", "conductivity", "emissivity"),
    "B": ("name", "conductivity", "emissivity"),
    "C": ("participant", "product", "conductivity"),
}

# Column count expected per appendix (after stripping empty trailing cols).
# A & B: Name, k(W/mK), k(Btu/hr-ft-F), k(Btu-in/hr-ft2-F), Source, Emissivity = 6
# C: Participant, Product, Density, k(W/mK), k(Btu/hr-ft-F), k(Btu-in/hr-ft2-F),
#    Emissivity, Expiration = 8
_EXPECTED_COLS: dict[str, tuple[int, ...]] = {
    "A": (5, 6, 7),
    "B": (5, 6, 7),
    "C": (7, 8, 9),
}


# ---------------------------------------------------------------------------
# Multi-page table merging & wrapped-row reconstruction
# ---------------------------------------------------------------------------


def _trim_trailing_empty_cells(rows: list[Row]) -> list[Row]:
    """Trim trailing empty columns that are consistent across all rows."""
    if not rows:
        return rows
    min_len = min(len(r) for r in rows)
    # Find rightmost column index that has any non-empty cell across all rows.
    last_useful = -1
    for col_idx in range(min_len):
        if any(row[col_idx] != "" for row in rows):
            last_useful = col_idx
    if last_useful < 0:
        return [[""] * 1 for _ in rows]
    return [row[: last_useful + 1] for row in rows]


def _merge_wrapped_rows(rows: list[Row]) -> list[Row]:
    """Reconstruct records whose rows wrapped across visual lines.

    Heuristic:
      * If a row is blank in the FIRST cell but has content in OTHER cells,
        AND the previous row has content in the first cell, treat this row
        as a continuation and merge: append " / " + cell_content to each
        previous cell.
      * If a row is blank in the first cell AND blank in ALL cells, drop it
        (it's just spacing).
      * If a row has content in the first cell, it starts a new record.
    """
    if not rows:
        return []

    merged: list[Row] = []
    for row in rows:
        if not row:
            continue
        first = row[0]
        rest = row[1:]
        if first == "":
            if all(c == "" for c in rest):
                # Pure spacer row.
                continue
            # Continuation of the previous row, if any.
            if merged:
                prev = merged[-1]
                # Align column counts.
                width = max(len(prev), len(row))
                prev_padded = prev + [""] * (width - len(prev))
                row_padded = row + [""] * (width - len(row))
                for i in range(width):
                    if row_padded[i]:
                        if prev_padded[i]:
                            prev_padded[i] = f"{prev_padded[i]} {row_padded[i]}"
                        else:
                            prev_padded[i] = row_padded[i]
                merged[-1] = prev_padded
                continue
        # Fresh row.
        merged.append(list(row))
    return merged


def _drop_repeated_header(rows: list[Row], header_keywords: tuple[str, ...]) -> list[Row]:
    """Drop rows that look like a repeated header (after the first one)."""
    if not rows:
        return []
    out: list[Row] = [rows[0]]
    for row in rows[1:]:
        if _looks_like_header(row, header_keywords):
            logger.debug("Dropping repeated header row: %r", row)
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_table_for_appendix(
    pdf_path: Path,
    rng: AppendixRange,
) -> ExtractedTable:
    """Extract a single merged table for the given appendix range.

    Algorithm:
      1. For each page in [start_page, end_page], find all tables.
      2. Keep tables whose first row matches the appendix's header keywords
         AND whose column count is in the appendix's expected set.
      3. For each kept table, drop the header row, normalize all cells.
      4. Concatenate all kept tables' data rows in page order.
      5. Drop repeated header rows that may appear mid-stream.
      6. Trim trailing empty columns.
      7. Merge wrapped rows.
      8. Return ExtractedTable.

    Raises:
        TableExtractionError: if no usable tables were found in the range.
    """
    letter = rng.letter.upper()
    keywords = _HEADER_KEYWORDS.get(letter, ("name", "conductivity"))
    strict_required = _STRICT_HEADER_REQUIRED.get(letter, keywords)
    expected_cols = _EXPECTED_COLS.get(letter, (5, 6, 7, 8, 9))
    pdf_path = Path(pdf_path)

    raw_header: Row = []
    collected_rows: list[Row] = []
    used_pages: list[int] = []
    notes: list[str] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num in range(rng.start_page, rng.end_page + 1):
            if page_num > len(pdf.pages):
                break
            page = pdf.pages[page_num - 1]
            tables = page.find_tables()
            if not tables:
                logger.debug("Page %d: no tables found.", page_num)
                continue
            for ti, t in enumerate(tables):
                rows = t.extract() or []
                if not rows:
                    continue
                # Normalize every row.
                norm_rows = [normalize_row(r) for r in rows]
                # Check first non-blank row to see if it's a header.
                first_useful = next(
                    (r for r in norm_rows if not is_blank_row(r)), None
                )
                if first_useful is None:
                    continue
                # STRICT check: the header must contain ALL required keywords.
                # This filters out unrelated tables that share a keyword
                # (e.g. Appendix A's gas-coefficient table on page 51).
                if not _is_strict_header_match(first_useful, strict_required):
                    logger.debug(
                        "Page %d table %d: header %r does not satisfy strict keywords %s, skipping.",
                        page_num, ti, first_useful, strict_required,
                    )
                    continue
                # Confirm column count.
                if len(first_useful) not in expected_cols:
                    logger.debug(
                        "Page %d table %d: column count %d not in expected %s, skipping.",
                        page_num, ti, len(first_useful), expected_cols,
                    )
                    continue
                # Capture header from the first matching table we see.
                if not raw_header:
                    raw_header = list(first_useful)
                # Drop header rows (the first AND any repeated ones) + blank rows.
                # Use the looser OR-match for repeated-header detection so we
                # still drop headers that lost a keyword on a later page.
                body_rows = [
                    r for r in norm_rows
                    if not is_blank_row(r) and not _looks_like_header(r, keywords)
                ]
                if body_rows:
                    collected_rows.extend(body_rows)
                    used_pages.append(page_num)
                    logger.debug(
                        "Page %d table %d: collected %d body rows.",
                        page_num, ti, len(body_rows),
                    )

    if not collected_rows:
        raise TableExtractionError(
            f"No usable tables found for Appendix {letter} on pages "
            f"{rng.start_page}-{rng.end_page}."
        )

    # Trim trailing empty columns and merge wrapped rows.
    collected_rows = _trim_trailing_empty_cells(collected_rows)
    if raw_header:
        # Pad/trim header to match data column count.
        data_width = len(collected_rows[0]) if collected_rows else 0
        if len(raw_header) < data_width:
            raw_header = raw_header + [""] * (data_width - len(raw_header))
        elif len(raw_header) > data_width:
            raw_header = raw_header[:data_width]
    collected_rows = _merge_wrapped_rows(collected_rows)

    # Build a normalized header (lowercased identifier-style).
    norm_header = _normalize_header_identifiers(raw_header)

    notes.append(f"Merged {len(used_pages)} page(s) of tables for Appendix {letter}.")
    if not used_pages:
        notes.append("WARNING: no tables matched the expected schema.")

    logger.info(
        "Appendix %s: extracted %d data rows from pages %s",
        letter, len(collected_rows), used_pages,
    )

    return ExtractedTable(
        appendix_letter=letter,
        header=norm_header,
        raw_header=raw_header,
        rows=collected_rows,
        source_pages=tuple(used_pages),
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Header identifier normalization
# ---------------------------------------------------------------------------

# Maps raw header substrings (case-insensitive) to canonical identifiers.
# This is what makes downstream parsing robust to header wording changes
# between NFRC revisions.
_HEADER_PATTERNS: list[tuple[str, str]] = [
    (r"participant", "participant"),
    (r"product", "product"),
    (r"density", "density_kgm3"),
    (r"conductivity", "conductivity"),
    (r"emissivity", "emissivity"),
    (r"expiration", "expiration_date"),
    (r"source", "source_ref"),
    (r"name", "material_name"),
    (r"gas", "material_name"),
]


def _normalize_header_identifiers(raw_header: Row) -> Row:
    """Convert raw header cells into stable lowercase identifiers.

    For each raw header cell, find the FIRST matching pattern and return its
    canonical identifier. Empty cells become "". Non-matching cells keep a
    slugified version of their text.

    Example: ['Name', 'Conductivity k', '', '', 'Source1', 'Emissivity ε']
          -> ['material_name', 'conductivity', '', '', 'source_ref', 'emissivity']
    """
    out: Row = []
    for cell in raw_header:
        if cell == "":
            out.append("")
            continue
        lower = cell.lower()
        matched = ""
        for pat, ident in _HEADER_PATTERNS:
            if re.search(pat, lower):
                matched = ident
                break
        if matched:
            out.append(matched)
        else:
            # Slugify.
            slug = re.sub(r"[^a-z0-9]+", "_", lower).strip("_")
            out.append(slug or "col")
    return out
