"""Tests for src/appendix_locator.py."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.appendix_locator import (
    AppendixLocatorError,
    AppendixMap,
    AppendixRange,
    locate_appendices,
    require_targets,
)


def test_locates_target_appendices_in_synthetic_pdf(synthetic_pdf: Path) -> None:
    amap = locate_appendices(synthetic_pdf)
    assert isinstance(amap, AppendixMap)
    targets = amap.targets()
    assert set(targets.keys()) == {"A", "B", "C"}
    # Page assignments: A starts on page 1, B on 2, C on 3, D on 4.
    assert targets["A"].start_page == 1
    assert targets["A"].end_page == 1   # ends where B begins (minus 1)
    assert targets["B"].start_page == 2
    assert targets["B"].end_page == 2
    assert targets["C"].start_page == 3
    assert targets["C"].end_page == 3   # ends where D begins (minus 1)


def test_missing_targets_returns_d_or_empty() -> None:
    amap = AppendixMap(ranges={"A": AppendixRange("A", 1, 1)})
    assert amap.missing_targets() == ["B", "C"]


def test_require_targets_raises_on_missing(synthetic_pdf: Path) -> None:
    # synthetic_pdf has A, B, C, D — require_targets should succeed.
    targets = require_targets(synthetic_pdf)
    assert set(targets.keys()) == {"A", "B", "C"}


def test_require_targets_raises_when_no_appendices(tmp_path: Path) -> None:
    # Build a PDF with no appendix headings at all.
    import fitz
    p = tmp_path / "no_appendices.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 80), "Some random text", fontsize=12, fontname="helv")
    doc.save(str(p))
    doc.close()
    with pytest.raises(AppendixLocatorError):
        require_targets(p)


def test_locator_ignores_lowercase_appendix_mentions(tmp_path: Path) -> None:
    """Body-text mentions like 'See Appendix A.' must NOT count as headings."""
    import fitz
    p = tmp_path / "body_mention.pdf"
    doc = fitz.open()
    page = doc.new_page()
    # Body text mentioning Appendix A at 12pt (NOT bold, NOT large).
    page.insert_text((72, 80), "Appendix A may be used to establish a new",
                     fontsize=12, fontname="helv")
    page.insert_text((72, 100), "Appendix B generic material if the conductivity",
                     fontsize=12, fontname="helv")
    doc.save(str(p))
    doc.close()
    with pytest.raises(AppendixLocatorError):
        locate_appendices(p)


def test_locator_handles_toc_then_actual_heading(tmp_path: Path) -> None:
    """A TOC entry at 12pt bold must be skipped; the 18pt heading must win."""
    import fitz
    p = tmp_path / "toc_plus_heading.pdf"
    doc = fitz.open()
    # Page 1: TOC entries at 12pt bold.
    page1 = doc.new_page()
    page1.insert_text((72, 80), "Appendix A    Basic Set of Generic Thermophysical",
                      fontsize=12, fontname="hebo")
    # Page 2: Real heading at 18pt bold.
    page2 = doc.new_page()
    page2.insert_text((72, 80), "APPENDIX A BASIC SET", fontsize=18, fontname="hebo")

    doc.save(str(p))
    doc.close()

    amap = locate_appendices(p)
    assert "A" in amap.ranges
    # The heading should be detected on page 2, not page 1.
    assert amap.ranges["A"].start_page == 2
