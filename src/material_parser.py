"""Material parser: turns raw table rows into structured JSON objects.

Each appendix has its own schema:

  * Appendix A & B materials (generic thermophysical properties):
      material_name       : str
      conductivity_wmk    : float | None      # W/m•K  (primary SI unit)
      conductivity_btu_hr_ft_f     : float | None
      conductivity_btu_in_hr_ft2_f : float | None
      source_ref          : str               # e.g. '1,3,15'
      emissivity          : float | None
      source_appendix     : 'A' | 'B'
      category            : str | None        # e.g. 'Elastomers', if detectable

  * Appendix C materials (proprietary thermophysical properties):
      participant         : str
      product             : str
      density_kgm3        : float | None
      conductivity_wmk    : float | None
      conductivity_btu_hr_ft_f     : float | None
      conductivity_btu_in_hr_ft2_f : float | None
      emissivity          : float | None
      expiration_date     : str | None        # ISO-8601 'YYYY-MM-DD'
      source_appendix     : 'C'

Parsing rules:
  * Numeric fields accept plain floats, integers, scientific notation
    ('1.2e-3'), and engineering notation ('2.873x10-3' -> 2.873e-3).
  * 'See Appendix A' / 'See Appendix B' is preserved as a string in a
    *_note field and the corresponding numeric field is set to None.
  * Density values with embedded annotations (e.g. '6.92 (at 1.25")') are
    parsed to their leading numeric portion; the annotation is preserved in
    `density_note`.
  * Dates in M/D/YYYY form (NFRC's convention) are normalized to ISO-8601
    'YYYY-MM-DD'. Invalid dates are kept as raw strings in
    `expiration_date_raw` and `expiration_date` is set to None.
  * Section header rows (single populated cell) are recorded as `category`
    context for the subsequent data rows.

The parser does NOT validate; it only structures. Validation lives in
`validator.py`.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from .table_extractor import (
    ExtractedTable,
    Row,
    is_section_header_row,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Numeric parsing
# ---------------------------------------------------------------------------

# Match engineering-style '2.873x10-3' or '2.873x10^-3' or '2.873E-3' or plain.
_ENG_RE = re.compile(
    r"^\s*([+-]?\d+(?:\.\d+)?)"          # mantissa
    r"\s*[xX]\s*10\s*\^?\s*([+-]?\d+)\s*$"  # exponent
)
# Plain float / int, possibly with embedded annotation like '6.92 (at 1.25")'.
_LEADING_NUM_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)")


def parse_engineering_number(text: str) -> float | None:
    """Parse strings like '2.873x10-3' or '1.0E-3' into floats.

    Returns None if the string cannot be parsed.
    """
    if not text:
        return None
    s = text.strip()
    # Try engineering notation first.
    m = _ENG_RE.match(s)
    if m:
        mantissa = float(m.group(1))
        exponent = int(m.group(2))
        return mantissa * (10.0 ** exponent)
    # Plain numeric.
    try:
        return float(s)
    except ValueError:
        # Try extracting a leading number (e.g. '6.92 (at 1.25")' -> 6.92).
        m2 = _LEADING_NUM_RE.match(s)
        if m2:
            try:
                return float(m2.group(1))
            except ValueError:
                return None
        return None


def parse_optional_number(text: str) -> tuple[float | None, str | None]:
    """Parse a numeric cell, returning (value, note).

    * If the cell is empty: returns (None, None).
    * If the cell is 'See Appendix X' or other non-numeric text:
      returns (None, text).
    * If the cell has a clean number: returns (float(text), None).
    * If the cell has a number with annotation: returns (number, annotation_text).
    """
    s = (text or "").strip()
    if not s:
        return None, None
    val = parse_engineering_number(s)
    if val is not None:
        # Was there an annotation? (i.e. extra text after the leading number)
        m = _LEADING_NUM_RE.match(s)
        if m and len(s) > len(m.group(0)):
            annotation = s[len(m.group(0)):].strip()
            return val, annotation
        return val, None
    # Non-numeric (e.g. 'See Appendix A', '-', 'N/A').
    return None, s


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

_DATE_FORMATS = (
    "%m/%d/%Y",   # 12/31/2029 (NFRC convention)
    "%-m/%-d/%Y", # 7/1/2029 (Linux-specific; we will handle manually)
    "%Y-%m-%d",   # already ISO
    "%m-%d-%Y",
    "%B %d %Y",
    "%b %d %Y",
)


def parse_date_to_iso(text: str) -> tuple[str | None, str | None]:
    """Parse a date string and return (iso_string, raw_input).

    Returns (None, raw) if the date cannot be parsed.
    """
    s = (text or "").strip()
    if not s or s == "-":
        # Both empty string and the NFRC '-' placeholder mean 'no date'.
        return None, None
    # Try M/D/YYYY with manual parsing (handles single-digit month/day).
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mm, dd, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            d = datetime(yyyy, mm, dd, tzinfo=None)
            return d.strftime("%Y-%m-%d"), s
        except ValueError:
            return None, s
    # Try ISO YYYY-MM-DD.
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return d.strftime("%Y-%m-%d"), s
        except ValueError:
            return None, s
    return None, s


# ---------------------------------------------------------------------------
# Column index resolution (robust to header wording changes)
# ---------------------------------------------------------------------------


def _resolve_columns(header: Row, target: str) -> int:
    """Find the index of `target` in the header. Returns -1 if not found.

    `target` is one of the canonical identifiers produced by
    `table_extractor._normalize_header_identifiers`.
    """
    for i, h in enumerate(header):
        if h == target:
            return i
    return -1


def _resolve_column_with_unit_fallback(
    header: Row,
    rows: list[Row],
    canonical: str,
    unit_hints: tuple[str, ...],
) -> int:
    """Resolve a column that may not have an explicit header.

    NFRC appendix A/B tables have ONE 'Conductivity' header that spans
    three columns (W/m•K, Btu/hr•ft•F, Btu•in/hr•ft²•°F). Only the FIRST
    column gets the 'conductivity' identifier; the others have empty headers.
    The unit row tells us which is which.

    We use the unit_hints (e.g. 'W/m', 'Btu/hr', 'Btu') to disambiguate.
    """
    # First, see if any header cell matches `canonical` directly.
    direct = _resolve_columns(header, canonical)
    if direct >= 0:
        return direct
    # Otherwise, look at the first row for unit hints.
    if not rows:
        return -1
    first = rows[0]
    for i, cell in enumerate(first):
        lower = cell.lower()
        for hint in unit_hints:
            if hint.lower() in lower:
                return i
    return -1


# ---------------------------------------------------------------------------
# Appendix A & B parsing
# ---------------------------------------------------------------------------


def _parse_appendix_a_or_b(
    table: ExtractedTable,
    letter: str,
) -> list[dict[str, Any]]:
    """Parse an Appendix A or B table into a list of material dicts.

    Both appendices share the same 6-column schema:
      Name, Conductivity (W/mK), Conductivity (Btu/hr-ft-F),
      Conductivity (Btu-in/hr-ft²-F), Source, Emissivity
    """
    out: list[dict[str, Any]] = []
    header = table.header
    rows = table.rows

    # Resolve column indices. For the three conductivity columns we need
    # to find them by unit-row inspection since the header has empty cells.
    name_idx = _resolve_columns(header, "material_name")
    src_idx = _resolve_columns(header, "source_ref")
    emis_idx = _resolve_columns(header, "emissivity")

    # If header didn't yield clean indices, fall back to expected positions.
    if name_idx < 0:
        name_idx = 0
    if src_idx < 0 and len(header) >= 5:
        src_idx = 4
    if emis_idx < 0 and len(header) >= 6:
        emis_idx = 5

    # For the 3 conductivity columns: assume positions 1, 2, 3 by convention
    # (W/mK, Btu/hr-ft-F, Btu-in/hr-ft2-F). Confirm by inspecting unit row.
    cond_wmk_idx = 1
    cond_btu1_idx = 2
    cond_btu2_idx = 3

    # If the first data row looks like a units row (e.g. starts with '' and
    # contains 'W/m'), drop it but use it to refine column assignments.
    if rows:
        first = rows[0]
        # Sometimes pdfplumber returns the units row as the first body row.
        if first and first[name_idx] == "" and any(
            "w/m" in c.lower() or "btu" in c.lower() for c in first
        ):
            # Use this row to refine.
            for i, cell in enumerate(first):
                lower = cell.lower()
                if "w/m" in lower:
                    cond_wmk_idx = i
                elif "btu/hr" in lower and "ft2" not in lower:
                    cond_btu1_idx = i
                elif "btu" in lower and "in" in lower and "ft2" in lower:
                    cond_btu2_idx = i
            # Drop the units row.
            rows = rows[1:]

    # Detect category header rows so we can annotate subsequent materials.
    current_category: str | None = None

    for row_idx, row in enumerate(rows, start=1):
        # Pad row to the header width.
        width = max(len(header), 6)
        row = row + [""] * (width - len(row))

        if not any(row):
            continue

        # Section header?
        if is_section_header_row(row):
            current_category = row[name_idx].strip() or None
            logger.debug("Appendix %s row %d: category=%r", letter, row_idx, current_category)
            continue

        name = row[name_idx].strip() if name_idx < len(row) else ""
        if not name:
            # Likely a continuation row that didn't merge, or a malformed row.
            logger.debug("Appendix %s row %d: empty name, skipping. row=%r", letter, row_idx, row)
            continue

        def _cell(idx: int) -> str:
            return row[idx] if 0 <= idx < len(row) else ""

        k_wmk, k_wmk_note = parse_optional_number(_cell(cond_wmk_idx))
        k_btu1, k_btu1_note = parse_optional_number(_cell(cond_btu1_idx))
        k_btu2, k_btu2_note = parse_optional_number(_cell(cond_btu2_idx))
        source = _cell(src_idx).strip() if src_idx >= 0 else ""
        emis, emis_note = parse_optional_number(_cell(emis_idx))

        # '-' in the emissivity column is the NFRC convention for 'default 0.9'.
        # We leave it as None but record a note so consumers can apply their
        # own defaulting rule.
        material: dict[str, Any] = {
            "material_name": name,
            "conductivity_wmk": k_wmk,
            "conductivity_btu_hr_ft_f": k_btu1,
            "conductivity_btu_in_hr_ft2_f": k_btu2,
            "source_ref": source,
            "emissivity": emis,
            "source_appendix": letter,
        }
        if current_category:
            material["category"] = current_category
        # Preserve non-numeric notes (e.g. 'See Appendix A').
        notes: dict[str, str] = {}
        if k_wmk_note:
            notes["conductivity_wmk_note"] = k_wmk_note
        if k_btu1_note:
            notes["conductivity_btu_hr_ft_f_note"] = k_btu1_note
        if k_btu2_note:
            notes["conductivity_btu_in_hr_ft2_f_note"] = k_btu2_note
        if emis_note:
            notes["emissivity_note"] = emis_note
        if notes:
            material["notes"] = notes
        out.append(material)

    return out


# ---------------------------------------------------------------------------
# Appendix C parsing
# ---------------------------------------------------------------------------


def _parse_appendix_c(table: ExtractedTable) -> list[dict[str, Any]]:
    """Parse an Appendix C table into a list of proprietary-material dicts.

    Schema:
      Participant, Product, Density (kg/m3), Conductivity (W/mK),
      Conductivity (Btu/hr-ft-F), Conductivity (Btu-in/hr-ft2-F),
      Emissivity, Expiration Date
    """
    out: list[dict[str, Any]] = []
    header = table.header
    rows = table.rows

    part_idx = _resolve_columns(header, "participant")
    prod_idx = _resolve_columns(header, "product")
    dens_idx = _resolve_columns(header, "density_kgm3")
    emis_idx = _resolve_columns(header, "emissivity")
    exp_idx = _resolve_columns(header, "expiration_date")

    # Fall back to expected positions if header detection missed.
    if part_idx < 0:
        part_idx = 0
    if prod_idx < 0:
        prod_idx = 1
    if dens_idx < 0:
        dens_idx = 2
    # Three conductivity columns starting at index 3.
    cond_wmk_idx = 3
    cond_btu1_idx = 4
    cond_btu2_idx = 5
    if emis_idx < 0:
        emis_idx = 6
    if exp_idx < 0:
        exp_idx = 7

    # Drop a units row if present at the top.
    if rows:
        first = rows[0]
        if first and first[part_idx] == "" and any(
            "kg/m" in c.lower() or "w/m" in c.lower() or "btu" in c.lower()
            for c in first if c
        ):
            rows = rows[1:]

    for row_idx, row in enumerate(rows, start=1):
        width = max(len(header), 8)
        row = row + [""] * (width - len(row))
        if not any(row):
            continue
        if is_section_header_row(row):
            # Appendix C doesn't normally have section headers, but be defensive.
            logger.debug("Appendix C row %d: section header, skipping. row=%r", row_idx, row)
            continue

        def _cell(idx: int) -> str:
            return row[idx] if 0 <= idx < len(row) else ""

        participant = _cell(part_idx).strip()
        product = _cell(prod_idx).strip()
        if not participant and not product:
            logger.debug("Appendix C row %d: empty participant+product, skipping. row=%r",
                         row_idx, row)
            continue

        dens, dens_note = parse_optional_number(_cell(dens_idx))
        k_wmk, k_wmk_note = parse_optional_number(_cell(cond_wmk_idx))
        k_btu1, k_btu1_note = parse_optional_number(_cell(cond_btu1_idx))
        k_btu2, k_btu2_note = parse_optional_number(_cell(cond_btu2_idx))
        emis, emis_note = parse_optional_number(_cell(emis_idx))
        exp_iso, exp_raw = parse_date_to_iso(_cell(exp_idx))

        material: dict[str, Any] = {
            "participant": participant,
            "product": product,
            "density_kgm3": dens,
            "conductivity_wmk": k_wmk,
            "conductivity_btu_hr_ft_f": k_btu1,
            "conductivity_btu_in_hr_ft2_f": k_btu2,
            "emissivity": emis,
            "expiration_date": exp_iso,
            "source_appendix": "C",
        }
        if exp_raw and not exp_iso:
            material["expiration_date_raw"] = exp_raw
        if dens_note:
            material["density_note"] = dens_note
        notes: dict[str, str] = {}
        if k_wmk_note:
            notes["conductivity_wmk_note"] = k_wmk_note
        if k_btu1_note:
            notes["conductivity_btu_hr_ft_f_note"] = k_btu1_note
        if k_btu2_note:
            notes["conductivity_btu_in_hr_ft2_f_note"] = k_btu2_note
        if emis_note:
            notes["emissivity_note"] = emis_note
        if notes:
            material["notes"] = notes
        out.append(material)

    return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_appendix_table(table: ExtractedTable) -> list[dict[str, Any]]:
    """Dispatch to the correct parser based on the appendix letter."""
    letter = table.appendix_letter.upper()
    if letter in ("A", "B"):
        return _parse_appendix_a_or_b(table, letter)
    if letter == "C":
        return _parse_appendix_c(table)
    raise ValueError(f"Unknown appendix letter: {letter!r}")
