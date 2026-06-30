"""Tests for src/table_extractor.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.appendix_locator import AppendixRange
from src.table_extractor import (
    ExtractedTable,
    is_blank_row,
    is_section_header_row,
    normalize_cell,
    normalize_row,
    extract_table_for_appendix,
    _merge_wrapped_rows,
    _drop_repeated_header,
)


def test_normalize_cell_strips_control_chars_and_whitespace() -> None:
    assert normalize_cell(None) == ""
    assert normalize_cell("  hello  ") == "hello"
    # Tabs are whitespace (collapsed to single space); newlines within a cell
    # represent a multi-line cell and are preserved as " / ".
    assert normalize_cell("a\tb\n c") == "a b / c"
    assert normalize_cell("CO\x002") == "CO2"


def test_normalize_row_normalizes_all_cells() -> None:
    assert normalize_row([None, "  a  ", "b\nc"]) == ["", "a", "b / c"]


def test_is_blank_row() -> None:
    assert is_blank_row(["", "", ""]) is True
    assert is_blank_row(["", "x", ""]) is False
    assert is_blank_row([]) is True


def test_is_section_header_row() -> None:
    assert is_section_header_row(["Elastomers", "", "", "", "", ""]) is True
    assert is_section_header_row(["Elastomers", "0.25", "", "", "", ""]) is False
    assert is_section_header_row(["", "", "", "", "", ""]) is False
    assert is_section_header_row([]) is False


def test_merge_wrapped_rows_basic() -> None:
    rows = [
        ["Butadiene", "0.250", "0.144", "1.733", "1,15", "0.9"],
        ["", "(continued)", "", "", "", ""],
        ["Foam Rubber", "0.060", "0.035", "0.416", "1,15", "0.9"],
    ]
    merged = _merge_wrapped_rows(rows)
    assert len(merged) == 2
    # The continuation row's empty first cell leaves the name unchanged.
    assert merged[0][0] == "Butadiene"
    # The continuation's "(continued)" is appended to the second column.
    assert merged[0][1] == "0.250 (continued)"
    assert merged[1][0] == "Foam Rubber"


def test_merge_wrapped_rows_drops_spacer_rows() -> None:
    rows = [
        ["Name1", "0.1", "", "", "1", "0.9"],
        ["", "", "", "", "", ""],
        ["Name2", "0.2", "", "", "2", "0.9"],
    ]
    merged = _merge_wrapped_rows(rows)
    assert len(merged) == 2
    assert merged[0][0] == "Name1"
    assert merged[1][0] == "Name2"


def test_drop_repeated_header() -> None:
    rows = [
        ["Name", "Conductivity k", "", "", "Source1", "Emissivity"],
        ["Mat1", "0.1", "", "", "1", "0.9"],
        ["Name", "Conductivity k", "", "", "Source1", "Emissivity"],  # repeated
        ["Mat2", "0.2", "", "", "2", "0.9"],
    ]
    out = _drop_repeated_header(rows, ("name", "conductivity"))
    assert len(out) == 3
    assert out[0][0] == "Name"
    assert out[1][0] == "Mat1"
    assert out[2][0] == "Mat2"


def test_extract_table_for_appendix_on_synthetic_pdf(synthetic_pdf_with_table: Path) -> None:
    """Run extract_table_for_appendix against the synthetic Appendix A PDF."""
    rng = AppendixRange(letter="A", start_page=1, end_page=1)
    table = extract_table_for_appendix(synthetic_pdf_with_table, rng)
    assert isinstance(table, ExtractedTable)
    assert table.appendix_letter == "A"
    # Header should be normalized.
    assert "material_name" in table.header
    assert "conductivity" in table.header
    # We expect at least the 2 material data rows (Butadiene, Butyl rubber).
    # (The section header row 'Elastomers' should be retained as a section
    # header row for the parser to consume.)
    material_rows = [r for r in table.rows if r[0] not in ("", "Elastomers")]
    assert len(material_rows) >= 2


def test_extract_raises_when_no_tables_match(tmp_path: Path) -> None:
    """If the appendix range contains no qualifying tables, raise."""
    import fitz
    p = tmp_path / "empty.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Just some text, no tables here.", fontsize=12)
    doc.save(str(p))
    doc.close()
    from src.table_extractor import TableExtractionError
    rng = AppendixRange(letter="A", start_page=1, end_page=1)
    with pytest.raises(TableExtractionError):
        extract_table_for_appendix(p, rng)
