"""Configuration for the NFRC 101 synchronization pipeline.

All runtime-tunable values are centralized here. The pipeline is fully
configurable via environment variables so that GitHub Actions (or any CI
system) can override behavior without code changes.

Environment variables:
    NFRC_SOURCE_URL        - The landing page that contains the current PDF link.
                             The pipeline will follow redirects to the actual PDF.
    NFRC_PDF_URL            - Direct PDF URL (overrides landing-page discovery).
    NFRC_LANDING_PAGE_URL   - Alias of NFRC_SOURCE_URL.
    NFRC_OUTPUT_DIR         - Directory for JSON outputs (default: repo /data).
    NFRC_PDF_CACHE_DIR      - Directory for downloaded PDF cache (default: /tmp).
    NFRC_HTTP_TIMEOUT       - HTTP timeout in seconds (default: 60).
    NFRC_HTTP_RETRIES       - Number of retry attempts on network errors (default: 3).
    NFRC_LOG_LEVEL          - Logging level (default: INFO).
    NFRC_FAIL_ON_VALIDATION - Whether validation failure exits non-zero (default: true).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


# Repository root is two levels up from this file: src/config.py -> repo root.
REPO_ROOT: Path = Path(__file__).resolve().parent.parent

# Default landing page that contains the link to the current NFRC 101 PDF.
# The pipeline scrapes this page to discover the current PDF URL.
DEFAULT_LANDING_PAGE_URL: str = "https://nfrccommunity.org/page/TD"

# Fallback PDF URL used when auto-discovery from the landing page fails
# (e.g. when the NFRC community site WAF blocks our IP range). This is the
# last-known-good direct URL to the published NFRC 101 PDF. Update it via PR
# when a new NFRC 101 revision is published and discovery is still blocked.
DEFAULT_FALLBACK_PDF_URL: str = (
    "https://cdn.ymaws.com/nfrccommunity.org/resource/resmgr/"
    "2026technicaldocs/NFRC_101-2026_E0A2.pdf"
)

# Default output directory (committed to git).
DEFAULT_OUTPUT_DIR: Path = REPO_ROOT / "data"

# Default PDF cache directory (NOT committed to git).
DEFAULT_PDF_CACHE_DIR: Path = Path(os.environ.get("HOME", "/tmp")) / ".nfrc101" / "cache"


@dataclass(frozen=True)
class Settings:
    """Immutable runtime settings for the pipeline."""

    landing_page_url: str = DEFAULT_LANDING_PAGE_URL
    pdf_url: str | None = None
    fallback_pdf_url: str | None = DEFAULT_FALLBACK_PDF_URL
    output_dir: Path = field(default_factory=lambda: DEFAULT_OUTPUT_DIR)
    pdf_cache_dir: Path = field(default_factory=lambda: DEFAULT_PDF_CACHE_DIR)
    http_timeout: int = 60
    http_retries: int = 3
    log_level: str = "INFO"
    fail_on_validation: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        """Build settings from environment variables with sensible defaults."""
        landing = (
            os.environ.get("NFRC_SOURCE_URL")
            or os.environ.get("NFRC_LANDING_PAGE_URL")
            or DEFAULT_LANDING_PAGE_URL
        )
        pdf_url = os.environ.get("NFRC_PDF_URL") or None
        # Allow the fallback to be disabled by setting NFRC_FALLBACK_PDF_URL="".
        fallback_env = os.environ.get("NFRC_FALLBACK_PDF_URL")
        if fallback_env is None:
            fallback = DEFAULT_FALLBACK_PDF_URL
        elif fallback_env == "":
            fallback = None
        else:
            fallback = fallback_env
        out_dir = Path(os.environ.get("NFRC_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
        cache_dir = Path(os.environ.get("NFRC_PDF_CACHE_DIR", str(DEFAULT_PDF_CACHE_DIR)))
        timeout = int(os.environ.get("NFRC_HTTP_TIMEOUT", "60"))
        retries = int(os.environ.get("NFRC_HTTP_RETRIES", "3"))
        log_level = os.environ.get("NFRC_LOG_LEVEL", "INFO")
        fail_on_validation = os.environ.get(
            "NFRC_FAIL_ON_VALIDATION", "true"
        ).lower() in {"1", "true", "yes", "on"}
        return cls(
            landing_page_url=landing,
            pdf_url=pdf_url,
            fallback_pdf_url=fallback,
            output_dir=out_dir,
            pdf_cache_dir=cache_dir,
            http_timeout=timeout,
            http_retries=retries,
            log_level=log_level,
            fail_on_validation=fail_on_validation,
        )


# Canonical data model field names (kept here so all modules agree on the schema).
class FieldName:
    """Canonical field names used in the JSON output."""

    # Common
    MATERIAL_NAME = "material_name"
    SOURCE_APPENDIX = "source_appendix"
    CATEGORY = "category"

    # Appendix A & B
    CONDUCTIVITY_WMK = "conductivity_wmk"
    CONDUCTIVITY_BTUHR_FT_F = "conductivity_btu_hr_ft_f"
    CONDUCTIVITY_BTU_IN_HR_FT2_F = "conductivity_btu_in_hr_ft2_f"
    SOURCE_REF = "source_ref"
    EMISSIVITY = "emissivity"

    # Appendix C
    PARTICIPANT = "participant"
    PRODUCT = "product"
    DENSITY_KGM3 = "density_kgm3"
    EXPIRATION_DATE = "expiration_date"


# Required-field sets per appendix, used by validator.py
REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "A": frozenset({FieldName.MATERIAL_NAME, FieldName.SOURCE_APPENDIX}),
    "B": frozenset({FieldName.MATERIAL_NAME, FieldName.SOURCE_APPENDIX}),
    "C": frozenset(
        {
            FieldName.PARTICIPANT,
            FieldName.PRODUCT,
            FieldName.SOURCE_APPENDIX,
        }
    ),
}

# Numeric fields that must parse as floats (when present).
NUMERIC_FIELDS: frozenset[str] = frozenset(
    {
        FieldName.CONDUCTIVITY_WMK,
        FieldName.CONDUCTIVITY_BTUHR_FT_F,
        FieldName.CONDUCTIVITY_BTU_IN_HR_FT2_F,
        FieldName.EMISSIVITY,
        FieldName.DENSITY_KGM3,
    }
)

# Date fields that must be ISO-8601 parseable (when present).
DATE_FIELDS: frozenset[str] = frozenset({FieldName.EXPIRATION_DATE})
