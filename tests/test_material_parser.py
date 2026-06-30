"""Tests for src/material_parser.py."""
from __future__ import annotations

import pytest

from src.material_parser import (
    parse_engineering_number,
    parse_optional_number,
    parse_date_to_iso,
    parse_appendix_table,
)
from src.table_extractor import ExtractedTable


def test_parse_engineering_number_plain_float() -> None:
    assert parse_engineering_number("0.250") == 0.25
    assert parse_engineering_number("50") == 50.0
    assert parse_engineering_number("66.0") == 66.0


def test_parse_engineering_number_with_x_10_notation() -> None:
    assert parse_engineering_number("2.873x10-3") == pytest.approx(2.873e-3)
    assert parse_engineering_number("2.873x10^-3") == pytest.approx(2.873e-3)
    assert parse_engineering_number("2.873X10-3") == pytest.approx(2.873e-3)


def test_parse_engineering_number_returns_none_for_garbage() -> None:
    assert parse_engineering_number("") is None
    assert parse_engineering_number("See Appendix A") is None
    assert parse_engineering_number("-") is None


def test_parse_optional_number_clean() -> None:
    val, note = parse_optional_number("0.250")
    assert val == 0.25
    assert note is None


def test_parse_optional_number_with_annotation() -> None:
    val, note = parse_optional_number('6.92 (at 1.25")')
    assert val == 6.92
    assert note == '(at 1.25")'


def test_parse_optional_number_see_appendix() -> None:
    val, note = parse_optional_number("See Appendix A")
    assert val is None
    assert note == "See Appendix A"


def test_parse_optional_number_empty() -> None:
    val, note = parse_optional_number("")
    assert val is None
    assert note is None


def test_parse_optional_number_dash_means_default() -> None:
    """A bare '-' in the emissivity column is NFRC's 'default' marker."""
    val, note = parse_optional_number("-")
    assert val is None
    assert note == "-"


def test_parse_date_to_iso_mdY() -> None:
    iso, raw = parse_date_to_iso("12/31/2029")
    assert iso == "2029-12-31"
    assert raw == "12/31/2029"


def test_parse_date_to_iso_single_digit_month_day() -> None:
    iso, _ = parse_date_to_iso("7/1/2026")
    assert iso == "2026-07-01"


def test_parse_date_to_iso_already_iso() -> None:
    iso, _ = parse_date_to_iso("2026-07-01")
    assert iso == "2026-07-01"


def test_parse_date_to_iso_invalid_returns_none() -> None:
    iso, raw = parse_date_to_iso("not a date")
    assert iso is None
    assert raw == "not a date"


def test_parse_date_to_iso_empty() -> None:
    iso, raw = parse_date_to_iso("")
    assert iso is None
    assert raw is None


def test_parse_date_to_iso_dash() -> None:
    iso, raw = parse_date_to_iso("-")
    assert iso is None
    # Per implementation, '-' is treated as empty-equivalent -> raw None.
    assert raw is None


def test_parse_date_to_iso_invalid_calendar_date() -> None:
    iso, raw = parse_date_to_iso("2026-02-31")  # Feb 31 doesn't exist
    # Our parser uses datetime() constructor which rejects invalid calendar
    # dates, so iso is None and raw preserves the input.
    assert iso is None
    assert raw == "2026-02-31"


# ---------------------------------------------------------------------------
# parse_appendix_table integration tests
# ---------------------------------------------------------------------------


def _make_appendix_a_table(rows: list[list[str]]) -> ExtractedTable:
    """Helper: build an ExtractedTable with the canonical Appendix A header."""
    header = ["material_name", "conductivity", "", "", "source_ref", "emissivity"]
    return ExtractedTable(
        appendix_letter="A",
        header=header,
        raw_header=["Name", "Conductivity k", "", "", "Source1", "Emissivity e"],
        rows=rows,
        source_pages=(1,),
    )


def test_parse_appendix_a_basic() -> None:
    table = _make_appendix_a_table([
        ["", "W/m-K", "Btu/hr-ft-F", "Btu-in/hr-ft2-F", "-", "-"],   # units row
        ["Elastomers", "", "", "", "", ""],                          # section header
        ["Butadiene", "0.250", "0.144", "1.733", "1,15", "0.9"],
        ["Butyl rubber", "0.240", "0.139", "1.664", "1,3,15", "0.9"],
    ])
    materials = parse_appendix_table(table)
    assert len(materials) == 2
    assert materials[0]["material_name"] == "Butadiene"
    assert materials[0]["conductivity_wmk"] == 0.25
    assert materials[0]["emissivity"] == 0.9
    assert materials[0]["source_ref"] == "1,15"
    assert materials[0]["source_appendix"] == "A"
    assert materials[0]["category"] == "Elastomers"
    assert materials[1]["material_name"] == "Butyl rubber"
    assert materials[1]["category"] == "Elastomers"


def test_parse_appendix_a_handles_see_appendix_a() -> None:
    """When a cell says 'See Appendix A', preserve as a note, value None."""
    # NB: This scenario actually arises in Appendix B, not A. We use the
    # Appendix A parser since A and B share the parser implementation.
    table = _make_appendix_a_table([
        ["Vulcanized rubber", "See Appendix A", "", "", "", ""],
    ])
    materials = parse_appendix_table(table)
    assert len(materials) == 1
    m = materials[0]
    assert m["conductivity_wmk"] is None
    assert m["notes"]["conductivity_wmk_note"] == "See Appendix A"


def test_parse_appendix_a_skips_blank_rows() -> None:
    table = _make_appendix_a_table([
        ["Mat1", "0.1", "", "", "1", "0.9"],
        ["", "", "", "", "", ""],   # blank row
        ["Mat2", "0.2", "", "", "2", "0.9"],
    ])
    materials = parse_appendix_table(table)
    assert len(materials) == 2


def test_parse_appendix_a_handles_dash_in_emissivity() -> None:
    table = _make_appendix_a_table([
        ["Mat1", "0.1", "0.06", "0.7", "1", "-"],
    ])
    materials = parse_appendix_table(table)
    m = materials[0]
    assert m["emissivity"] is None
    assert m["notes"]["emissivity_note"] == "-"


def _make_appendix_c_table(rows: list[list[str]]) -> ExtractedTable:
    header = [
        "participant", "product", "density_kgm3",
        "conductivity", "", "",
        "emissivity", "expiration_date",
    ]
    return ExtractedTable(
        appendix_letter="C",
        header=header,
        raw_header=[
            "Participant", "Product", "Density", "Conductivity",
            "", "", "Emissivity", "Expiration Date",
        ],
        rows=rows,
        source_pages=(1,),
    )


def test_parse_appendix_c_basic() -> None:
    table = _make_appendix_c_table([
        ["", "", "kg/m3", "W/m-K", "Btu/hr-ft-F", "Btu-in/hr-ft2-F", "", ""],  # units row
        ["3M", "VHB Tape B23F", "720", "0.181", "0.105", "1.26", "-", "12/31/2029"],
    ])
    materials = parse_appendix_table(table)
    assert len(materials) == 1
    m = materials[0]
    assert m["participant"] == "3M"
    assert m["product"] == "VHB Tape B23F"
    assert m["density_kgm3"] == 720
    assert m["conductivity_wmk"] == 0.181
    assert m["emissivity"] is None
    assert m["expiration_date"] == "2029-12-31"
    assert m["source_appendix"] == "C"


def test_parse_appendix_c_with_density_annotation() -> None:
    table = _make_appendix_c_table([
        ["Kingspan", "IMG 125", '6.92 (at 1.25")', "0.037", "0.021", "0.258", "-", "12/31/2029"],
    ])
    materials = parse_appendix_table(table)
    m = materials[0]
    assert m["density_kgm3"] == 6.92
    assert m["density_note"] == '(at 1.25")'


def test_parse_appendix_c_invalid_date_preserved_in_raw() -> None:
    table = _make_appendix_c_table([
        ["Foo", "Bar", "100", "0.1", "0.06", "0.7", "0.9", "not-a-date"],
    ])
    materials = parse_appendix_table(table)
    m = materials[0]
    assert m["expiration_date"] is None
    assert m["expiration_date_raw"] == "not-a-date"
