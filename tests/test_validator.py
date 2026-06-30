"""Tests for src/validator.py."""
from __future__ import annotations

import pytest

from src.validator import (
    AppendixStats,
    ValidationReport,
    validate_all,
)


def test_validate_all_empty_returns_ok_with_warnings() -> None:
    report = validate_all({"A": [], "B": [], "C": []})
    assert isinstance(report, ValidationReport)
    # Empty appendices produce warnings, not errors.
    assert report.ok is True
    assert report.warning_count >= 3
    assert report.error_count == 0
    assert report.stats["A"].count == 0
    assert report.stats["B"].count == 0
    assert report.stats["C"].count == 0


def test_validate_clean_appendix_a() -> None:
    materials_a = [
        {
            "material_name": "Butadiene",
            "conductivity_wmk": 0.25,
            "emissivity": 0.9,
            "source_ref": "1,15",
            "source_appendix": "A",
        },
        {
            "material_name": "Butyl rubber",
            "conductivity_wmk": 0.24,
            "emissivity": 0.9,
            "source_ref": "1,3,15",
            "source_appendix": "A",
        },
    ]
    report = validate_all({"A": materials_a, "B": [], "C": []})
    assert report.ok is True
    assert report.error_count == 0
    assert report.stats["A"].count == 2


def test_validate_detects_duplicates_appendix_a() -> None:
    materials_a = [
        {"material_name": "Butadiene", "source_appendix": "A", "conductivity_wmk": 0.25, "emissivity": 0.9},
        {"material_name": "butadiene", "source_appendix": "A", "conductivity_wmk": 0.25, "emissivity": 0.9},  # dup
    ]
    report = validate_all({"A": materials_a, "B": [], "C": []})
    assert report.ok is False
    assert any(i.code == "duplicate_material" for i in report.issues)


def test_validate_detects_duplicates_appendix_c() -> None:
    materials_c = [
        {"participant": "3M", "product": "VHB Tape", "source_appendix": "C"},
        {"participant": "3M", "product": "VHB Tape", "source_appendix": "C"},  # dup
    ]
    report = validate_all({"A": [], "B": [], "C": materials_c})
    assert report.ok is False
    assert any(i.code == "duplicate_material" for i in report.issues)


def test_validate_detects_missing_required_field_appendix_a() -> None:
    materials_a = [
        # Missing source_appendix -> ERROR.
        {"material_name": "Mat1", "conductivity_wmk": 0.25, "emissivity": 0.9},
    ]
    report = validate_all({"A": materials_a, "B": [], "C": []})
    assert report.ok is False
    assert any(
        i.code == "missing_required_field" and i.appendix == "A"
        for i in report.issues
    )


def test_validate_detects_missing_required_field_appendix_c() -> None:
    materials_c = [
        # Missing participant and product.
        {"source_appendix": "C"},
    ]
    report = validate_all({"A": [], "B": [], "C": materials_c})
    assert report.ok is False
    # Should produce multiple missing-field errors (participant, product).
    missing = [i for i in report.issues if i.code == "missing_required_field"]
    assert len(missing) >= 2


def test_validate_detects_invalid_numeric_type() -> None:
    materials_a = [
        {
            "material_name": "Mat1",
            "source_appendix": "A",
            "conductivity_wmk": "not a number",  # invalid type
            "emissivity": 0.9,
        },
    ]
    report = validate_all({"A": materials_a, "B": [], "C": []})
    assert report.ok is False
    assert any(i.code == "invalid_numeric_type" for i in report.issues)


def test_validate_detects_nan_numeric() -> None:
    import math
    materials_a = [
        {
            "material_name": "Mat1",
            "source_appendix": "A",
            "conductivity_wmk": float("nan"),
            "emissivity": 0.9,
        },
    ]
    report = validate_all({"A": materials_a, "B": [], "C": []})
    assert report.ok is False
    assert any(i.code == "invalid_numeric_value" for i in report.issues)


def test_validate_detects_invalid_date_format() -> None:
    materials_c = [
        {
            "participant": "Foo",
            "product": "Bar",
            "source_appendix": "C",
            "expiration_date": "not-a-date",
        },
    ]
    report = validate_all({"A": [], "B": [], "C": materials_c})
    assert report.ok is False
    assert any(i.code == "invalid_date_format" for i in report.issues)


def test_validate_detects_invalid_calendar_date() -> None:
    materials_c = [
        {
            "participant": "Foo",
            "product": "Bar",
            "source_appendix": "C",
            "expiration_date": "2026-02-31",  # Feb 31 doesn't exist
        },
    ]
    report = validate_all({"A": [], "B": [], "C": materials_c})
    assert report.ok is False
    assert any(i.code == "invalid_date_value" for i in report.issues)


def test_validate_sanity_range_warnings() -> None:
    """Emissivity > 1.0 should produce a WARNING (not error) by default."""
    materials_a = [
        {
            "material_name": "Mat1",
            "source_appendix": "A",
            "conductivity_wmk": 0.25,
            "emissivity": 1.5,  # outside [0, 1]
        },
    ]
    report = validate_all({"A": materials_a, "B": [], "C": []})
    # Should be a warning, not an error -> ok stays True.
    assert report.ok is True
    assert any(
        i.code == "value_outside_expected_range" and i.severity == "WARNING"
        for i in report.issues
    )


def test_validate_strict_sanity_promotes_warnings_to_errors() -> None:
    materials_a = [
        {
            "material_name": "Mat1",
            "source_appendix": "A",
            "conductivity_wmk": 0.25,
            "emissivity": 1.5,
        },
    ]
    report = validate_all({"A": materials_a, "B": [], "C": []}, strict_sanity=True)
    assert report.ok is False
    assert any(
        i.code == "value_outside_expected_range" and i.severity == "ERROR"
        for i in report.issues
    )


def test_validate_report_to_dict_structure() -> None:
    report = validate_all({"A": [], "B": [], "C": []})
    d = report.to_dict()
    assert "ok" in d
    assert "error_count" in d
    assert "warning_count" in d
    assert "appendix_a_count" in d
    assert "appendix_b_count" in d
    assert "appendix_c_count" in d
    assert "stats" in d
    assert "issues" in d
    assert isinstance(d["issues"], list)
