"""Shared pytest fixtures for the NFRC 101 sync test suite."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def live_pdf_path() -> Path | None:
    """Path to the live NFRC 101 PDF if it has been downloaded for testing.

    Tests that depend on this fixture are skipped if the PDF is not present.
    To populate it locally:
        python scripts/analyze_pdf.py
    which downloads the PDF to scripts/nfrc101.pdf.
    """
    candidates = [
        FIXTURES_DIR / "nfrc101.pdf",
        Path(__file__).resolve().parent.parent / "scripts" / "nfrc101.pdf",
    ]
    for c in candidates:
        if c.exists() and c.stat().st_size > 0:
            return c
    return None


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


def make_synthetic_pdf(path: Path, pages: list[list[dict]]) -> Path:
    """Create a small synthetic PDF for testing.

    Each entry in `pages` is a list of "span" dicts with keys:
        text, size, font, x, y
    The PDF will contain those spans laid out top-to-bottom.

    This lets us write unit tests for heading detection and table extraction
    without depending on the real NFRC 101 PDF.
    """
    import fitz

    doc = fitz.open()
    for spans in pages:
        page = doc.new_page(width=612, height=792)
        for s in spans:
            text = s.get("text", "")
            size = float(s.get("size", 12))
            font = s.get("font", "helv")
            x = float(s.get("x", 72))
            y = float(s.get("y", 72))
            page.insert_text((x, y), text, fontsize=size, fontname=font)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture
def synthetic_pdf(tmp_path) -> Path:
    """A minimal synthetic PDF with one appendix heading."""
    out = tmp_path / "synthetic.pdf"
    make_synthetic_pdf(
        out,
        pages=[
            [
                {"text": "APPENDIX A BASIC SET OF GENERIC", "size": 18, "font": "hebo", "x": 72, "y": 80},
                {"text": "THERMOPHYSICAL PROPERTIES", "size": 18, "font": "hebo", "x": 72, "y": 100},
                {"text": "Some intro text about materials.", "size": 12, "font": "helv", "x": 72, "y": 130},
            ],
            [
                {"text": "APPENDIX B EXTENDED SET", "size": 18, "font": "hebo", "x": 72, "y": 80},
            ],
            [
                {"text": "APPENDIX C PROPRIETARY MATERIALS", "size": 18, "font": "hebo", "x": 72, "y": 80},
            ],
            [
                {"text": "APPENDIX D MOISTURE CONTENT OF WOOD", "size": 18, "font": "hebo", "x": 72, "y": 80},
            ],
        ],
    )
    return out


@pytest.fixture
def synthetic_pdf_with_table(tmp_path) -> Path:
    """A synthetic PDF that mimics an Appendix A page: heading + a small table.

    We draw actual cell borders so pdfplumber's table detector finds them
    (pdfplumber relies on visible lines/rectangles to detect tables by default).
    """
    out = tmp_path / "appendix_a.pdf"
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=612, height=792)

    # Heading
    page.insert_text((72, 80), "APPENDIX A BASIC SET", fontsize=18, fontname="hebo")

    # Define the table layout. Use 6 columns starting at x=72, each 90 pts wide.
    rows = [
        ["Name", "Conductivity k", "", "", "Source1", "Emissivity"],
        ["", "W/m-K", "Btu/hr-ft-F", "Btu-in/hr-ft2-F", "-", "-"],
        ["Elastomers", "", "", "", "", ""],
        ["Butadiene", "0.250", "0.144", "1.733", "1,15", "0.9"],
        ["Butyl rubber", "0.240", "0.139", "1.664", "1,3,15", "0.9"],
    ]
    n_cols = 6
    col_w = 90.0
    row_h = 18.0
    x0 = 72.0
    y0 = 120.0

    # Draw cell borders (rectangles) and text.
    for r_idx, row in enumerate(rows):
        for c_idx in range(n_cols):
            rect = fitz.Rect(
                x0 + c_idx * col_w,
                y0 + r_idx * row_h,
                x0 + (c_idx + 1) * col_w,
                y0 + (r_idx + 1) * row_h,
            )
            page.draw_rect(rect, color=(0, 0, 0), width=0.5)
            cell_text = row[c_idx] if c_idx < len(row) else ""
            if cell_text:
                page.insert_text(
                    (rect.x0 + 2, rect.y1 - 4),
                    cell_text,
                    fontsize=9,
                    fontname="helv",
                )

    doc.save(str(out))
    doc.close()
    return out


def sha256_of_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
