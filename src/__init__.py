"""NFRC 101 Materials Library Synchronization System.

This package implements an automated pipeline that:
  1. Monitors the NFRC 101 published PDF for revisions.
  2. Downloads and fingerprints the PDF (SHA256).
  3. Locates Appendices A, B, and C by heading detection (no fixed page numbers).
  4. Extracts tables tolerating multi-page continuations and wrapped rows.
  5. Parses materials into structured JSON.
  6. Validates the generated data.

JSON is the source of truth. The architecture permits future XML exports via
a thin adapter, but XML is intentionally not part of the primary implementation.
"""

__version__ = "1.0.0"
__all__ = [
    "config",
    "monitor",
    "pdf_downloader",
    "appendix_locator",
    "table_extractor",
    "material_parser",
    "validator",
]
