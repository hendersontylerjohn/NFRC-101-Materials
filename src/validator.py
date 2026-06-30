"""Validator: checks parsed materials for structural and semantic problems.

Validation categories (all reported, none silently swallowed):
  1. Duplicate materials — same name (Appendix A/B) or same participant+product
     (Appendix C).
  2. Missing required fields — per-appendix required-field sets from config.py.
  3. Invalid numeric values — non-float in numeric fields (the parser already
     converts to float | None, so this catches NaN/Inf).
  4. Invalid dates — `expiration_date` must be ISO-8601 YYYY-MM-DD when present.
  5. Sanity ranges — conductivity, emissivity, density within plausible bounds.
     These produce WARNINGS, not errors, unless `strict_sanity` is True.

The validator returns a `ValidationReport`. The caller decides whether to
fail the workflow based on `report.ok` and `settings.fail_on_validation`.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import config

logger = logging.getLogger(__name__)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


# Sanity ranges (WARN-level). Source: NFRC 101 typical values + general physics.
_SANITY_RANGES: dict[str, tuple[float, float]] = {
    config.FieldName.CONDUCTIVITY_WMK: (0.0, 500.0),       # W/m•K
    config.FieldName.CONDUCTIVITY_BTUHR_FT_F: (0.0, 290.0),
    config.FieldName.CONDUCTIVITY_BTU_IN_HR_FT2_F: (0.0, 3500.0),
    config.FieldName.EMISSIVITY: (0.0, 1.0),
    config.FieldName.DENSITY_KGM3: (0.0, 25000.0),         # kg/m³
}


@dataclass(frozen=True)
class ValidationIssue:
    """A single validation issue."""

    severity: str  # "ERROR" or "WARNING"
    code: str      # short stable code, e.g. 'duplicate_material'
    appendix: str
    index: int     # 0-indexed position in the appendix's materials array
    message: str
    material_name: str | None = None  # for reporting convenience

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "appendix": self.appendix,
            "index": self.index,
            "message": self.message,
            "material_name": self.material_name,
        }


@dataclass
class AppendixStats:
    """Per-appendix aggregate statistics."""

    appendix: str
    count: int = 0
    error_count: int = 0
    warning_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "appendix": self.appendix,
            "count": self.count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
        }


@dataclass
class ValidationReport:
    """Full validation report across all appendices."""

    issues: list[ValidationIssue] = field(default_factory=list)
    stats: dict[str, AppendixStats] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True iff no ERROR-severity issues were found."""
        return not any(i.severity == "ERROR" for i in self.issues)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "ERROR")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "WARNING")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "appendix_a_count": self.stats.get("A", AppendixStats("A")).count,
            "appendix_b_count": self.stats.get("B", AppendixStats("B")).count,
            "appendix_c_count": self.stats.get("C", AppendixStats("C")).count,
            "stats": {k: v.to_dict() for k, v in self.stats.items()},
            "issues": [i.to_dict() for i in self.issues],
        }


# ---------------------------------------------------------------------------
# Individual check functions
# ---------------------------------------------------------------------------


def _material_display_name(material: dict[str, Any], appendix: str) -> str:
    """Return a human-friendly identifier for the material for error messages."""
    if appendix == "C":
        p = material.get("participant", "")
        pr = material.get("product", "")
        return f"{p} / {pr}".strip(" /")
    return str(material.get("material_name", ""))


def _check_duplicates(
    appendix: str,
    materials: list[dict[str, Any]],
    report: ValidationReport,
) -> None:
    """Detect duplicate materials within a single appendix.

    For A/B: duplicate = same `material_name` (case-insensitive, trimmed).
    For C:   duplicate = same `participant` + `product` (case-insensitive, trimmed).
    """
    seen: dict[str, int] = {}
    for idx, m in enumerate(materials):
        if appendix == "C":
            key = f"{m.get('participant', '').strip().lower()}||{m.get('product', '').strip().lower()}"
        else:
            key = str(m.get("material_name", "")).strip().lower()
        if not key or key == "||":
            continue
        if key in seen:
            prev_idx = seen[key]
            report.issues.append(
                ValidationIssue(
                    severity="ERROR",
                    code="duplicate_material",
                    appendix=appendix,
                    index=idx,
                    message=(
                        f"Duplicate material {_material_display_name(m, appendix)!r} "
                        f"(also at index {prev_idx})"
                    ),
                    material_name=_material_display_name(m, appendix),
                )
            )
        else:
            seen[key] = idx


def _check_required_fields(
    appendix: str,
    materials: list[dict[str, Any]],
    report: ValidationReport,
) -> None:
    """Check that every material has all required fields populated."""
    required = config.REQUIRED_FIELDS.get(appendix, frozenset())
    for idx, m in enumerate(materials):
        for fname in required:
            v = m.get(fname)
            if v is None or (isinstance(v, str) and v.strip() == ""):
                report.issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="missing_required_field",
                        appendix=appendix,
                        index=idx,
                        message=(
                            f"Material {_material_display_name(m, appendix)!r} "
                            f"is missing required field {fname!r}"
                        ),
                        material_name=_material_display_name(m, appendix),
                    )
                )


def _check_numeric_validity(
    appendix: str,
    materials: list[dict[str, Any]],
    report: ValidationReport,
) -> None:
    """Check that numeric fields are floats (not NaN/Inf)."""
    for idx, m in enumerate(materials):
        for fname in config.NUMERIC_FIELDS:
            if fname not in m:
                continue
            v = m.get(fname)
            if v is None:
                continue
            if not isinstance(v, (int, float)):
                report.issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="invalid_numeric_type",
                        appendix=appendix,
                        index=idx,
                        message=(
                            f"Material {_material_display_name(m, appendix)!r}: "
                            f"field {fname!r} is not numeric (got {type(v).__name__})"
                        ),
                        material_name=_material_display_name(m, appendix),
                    )
                )
                continue
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                report.issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="invalid_numeric_value",
                        appendix=appendix,
                        index=idx,
                        message=(
                            f"Material {_material_display_name(m, appendix)!r}: "
                            f"field {fname!r} is {v!r}"
                        ),
                        material_name=_material_display_name(m, appendix),
                    )
                )


def _check_dates(
    appendix: str,
    materials: list[dict[str, Any]],
    report: ValidationReport,
) -> None:
    """Check that date fields are ISO-8601 YYYY-MM-DD strings (or None)."""
    for idx, m in enumerate(materials):
        for fname in config.DATE_FIELDS:
            if fname not in m:
                continue
            v = m.get(fname)
            if v is None or v == "":
                continue
            if not isinstance(v, str):
                report.issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="invalid_date_type",
                        appendix=appendix,
                        index=idx,
                        message=(
                            f"Material {_material_display_name(m, appendix)!r}: "
                            f"date field {fname!r} is not a string (got {type(v).__name__})"
                        ),
                        material_name=_material_display_name(m, appendix),
                    )
                )
                continue
            if not _ISO_DATE_RE.match(v):
                report.issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="invalid_date_format",
                        appendix=appendix,
                        index=idx,
                        message=(
                            f"Material {_material_display_name(m, appendix)!r}: "
                            f"date field {fname!r}={v!r} is not ISO-8601 YYYY-MM-DD"
                        ),
                        material_name=_material_display_name(m, appendix),
                    )
                )
                continue
            # Try to actually parse it to catch invalid dates like 2026-02-31.
            try:
                datetime.strptime(v, "%Y-%m-%d")
            except ValueError:
                report.issues.append(
                    ValidationIssue(
                        severity="ERROR",
                        code="invalid_date_value",
                        appendix=appendix,
                        index=idx,
                        message=(
                            f"Material {_material_display_name(m, appendix)!r}: "
                            f"date field {fname!r}={v!r} is not a real calendar date"
                        ),
                        material_name=_material_display_name(m, appendix),
                    )
                )


def _check_sanity_ranges(
    appendix: str,
    materials: list[dict[str, Any]],
    report: ValidationReport,
) -> None:
    """WARN if numeric values fall outside plausible physical ranges."""
    for idx, m in enumerate(materials):
        for fname, (low, high) in _SANITY_RANGES.items():
            if fname not in m:
                continue
            v = m.get(fname)
            if not isinstance(v, (int, float)):
                continue
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                continue
            if v < low or v > high:
                report.issues.append(
                    ValidationIssue(
                        severity="WARNING",
                        code="value_outside_expected_range",
                        appendix=appendix,
                        index=idx,
                        message=(
                            f"Material {_material_display_name(m, appendix)!r}: "
                            f"field {fname!r}={v!r} is outside expected range "
                            f"[{low}, {high}]"
                        ),
                        material_name=_material_display_name(m, appendix),
                    )
                )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate_all(
    materials_by_appendix: dict[str, list[dict[str, Any]]],
    *,
    strict_sanity: bool = False,
) -> ValidationReport:
    """Validate materials for all target appendices.

    Args:
        materials_by_appendix: dict mapping 'A', 'B', 'C' to lists of materials.
        strict_sanity: if True, sanity-range warnings become errors.

    Returns:
        ValidationReport with all issues recorded.
    """
    report = ValidationReport()

    for letter in ("A", "B", "C"):
        materials = materials_by_appendix.get(letter, [])
        stats = AppendixStats(appendix=letter, count=len(materials))
        report.stats[letter] = stats

        if not materials:
            report.issues.append(
                ValidationIssue(
                    severity="WARNING",
                    code="empty_appendix",
                    appendix=letter,
                    index=-1,
                    message=f"Appendix {letter} produced 0 materials.",
                )
            )
            continue

        _check_duplicates(letter, materials, report)
        _check_required_fields(letter, materials, report)
        _check_numeric_validity(letter, materials, report)
        _check_dates(letter, materials, report)
        _check_sanity_ranges(letter, materials, report)

        # Re-classify warnings as errors if strict_sanity is on.
        if strict_sanity:
            for issue in report.issues:
                if issue.appendix == letter and issue.severity == "WARNING":
                    object.__setattr__(issue, "severity", "ERROR")  # frozen dataclass

        stats.error_count = sum(
            1 for i in report.issues
            if i.appendix == letter and i.severity == "ERROR"
        )
        stats.warning_count = sum(
            1 for i in report.issues
            if i.appendix == letter and i.severity == "WARNING"
        )

    logger.info(
        "Validation complete: %d errors, %d warnings, ok=%s",
        report.error_count, report.warning_count, report.ok,
    )
    return report
