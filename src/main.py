"""Main orchestrator: runs the full NFRC 101 sync pipeline.

Usage (from repository root):
    python -m src.main                # full run with environment config
    python -m src.main --force        # reprocess even if PDF unchanged
    python -m src.main --no-fail      # don't exit non-zero on validation errors

The pipeline:
    1. Resolve the current NFRC 101 PDF URL (from env or by scraping the
       landing page).
    2. Download the PDF and compute its SHA256.
    3. Load previous metadata; decide whether to reprocess.
    4. If unchanged and not --force: exit 0 (no commit needed).
    5. Locate Appendices A, B, C by heading detection.
    6. Extract tables for each appendix.
    7. Parse tables into structured JSON.
    8. Validate.
    9. Write data/appendix_a.json, data/appendix_b.json, data/appendix_c.json,
       data/metadata.json, and data/validation_report.json.
   10. Exit 0 if validation OK (or --no-fail); exit 1 otherwise.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import config
from .appendix_locator import AppendixRange, require_targets
from .material_parser import parse_appendix_table
from .monitor import (
    ChangeDecision,
    MetadataRecord,
    decide,
    load_metadata,
    metadata_path,
    save_metadata,
)
from .pdf_downloader import DownloadResult, discover_pdf_url, download_pdf
from .table_extractor import extract_table_for_appendix
from .validator import validate_all


logger = logging.getLogger("nfrc101.main")


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )


def _resolve_pdf_url(settings: config.Settings) -> str:
    """Determine the PDF URL to download.

    Resolution order:
      1. NFRC_PDF_URL (explicit env override) — highest priority.
      2. Auto-discovery from NFRC_LANDING_PAGE_URL (scrapes the page for an
         NFRC_101*.pdf link).
      3. NFRC_FALLBACK_PDF_URL (last-known-good direct URL) — used when the
         landing page is unreachable or behind a WAF that blocks our requests.

    The fallback is critical because the NFRC community site (Higher Logic /
    YMaws) returns HTTP 403 to non-browser clients from many cloud/CI IP
    ranges. The CDN serving the PDF itself is permissive.
    """
    if settings.pdf_url:
        logger.info("Using PDF URL from environment: %s", settings.pdf_url)
        return settings.pdf_url

    try:
        url = discover_pdf_url(
            settings.landing_page_url,
            timeout=settings.http_timeout,
            retries=settings.http_retries,
        )
        logger.info("Auto-discovered PDF URL: %s", url)
        return url
    except Exception as e:
        logger.warning(
            "Auto-discovery from %s failed: %s", settings.landing_page_url, e
        )
        if settings.fallback_pdf_url:
            logger.warning(
                "Falling back to NFRC_FALLBACK_PDF_URL=%s. "
                "If this URL is stale, set NFRC_PDF_URL or update the fallback "
                "in src/config.py.",
                settings.fallback_pdf_url,
            )
            return settings.fallback_pdf_url
        raise


def _write_json(path: Path, data: Any) -> None:
    """Write JSON with stable formatting (sorted keys, 2-space indent, trailing newline)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)
    logger.info("Wrote %s", path)


def _process_appendix(
    pdf_path: Path,
    rng: AppendixRange,
) -> list[dict[str, Any]]:
    """Extract + parse a single appendix; returns its materials list."""
    table = extract_table_for_appendix(pdf_path, rng)
    materials = parse_appendix_table(table)
    logger.info(
        "Appendix %s: %d materials parsed from %d table rows",
        rng.letter, len(materials), len(table.rows),
    )
    return materials


def run_pipeline(settings: config.Settings, *, force: bool = False) -> int:
    """Execute the full pipeline. Returns process exit code (0 success, 1 failure)."""
    _setup_logging(settings.log_level)
    logger.info("NFRC 101 sync starting. output_dir=%s", settings.output_dir)

    # 1. Resolve URL.
    try:
        pdf_url = _resolve_pdf_url(settings)
    except Exception as e:
        logger.error("Failed to resolve PDF URL: %s", e)
        return 1

    # 2. Download.
    try:
        download: DownloadResult = download_pdf(
            pdf_url,
            dest_dir=settings.pdf_cache_dir,
            timeout=settings.http_timeout,
            retries=settings.http_retries,
        )
    except Exception as e:
        logger.error("Failed to download PDF: %s", e)
        return 1

    # 3. Load previous metadata & decide.
    meta_path = metadata_path(settings)
    previous = load_metadata(meta_path)
    decision: ChangeDecision = decide(
        download=download,
        source_url=settings.landing_page_url,
        previous=previous,
    )

    if not decision.changed and not force:
        logger.info("No change detected: %s. Exiting successfully without committing.",
                    decision.reason)
        # Even on no-change, refresh last_checked timestamp on disk so users
        # can see when we last looked. (This MUST NOT trigger a commit because
        # the GitHub Actions workflow only commits when data/ JSON files
        # actually differ in git.)
        save_metadata(meta_path, decision.current)
        return 0

    if force and not decision.changed:
        logger.info("Force reprocessing despite no change detected.")
    else:
        logger.info("Change detected: %s", decision.reason)

    # 4. Locate appendices.
    try:
        targets = require_targets(download.local_path)
    except Exception as e:
        logger.error("Appendix location failed: %s", e)
        return 1

    # 5. Extract & parse each appendix.
    materials_by_appendix: dict[str, list[dict[str, Any]]] = {}
    try:
        for letter in ("A", "B", "C"):
            rng = targets[letter]
            materials_by_appendix[letter] = _process_appendix(download.local_path, rng)
    except Exception as e:
        logger.error("Extraction/parsing failed: %s", e)
        return 1

    # 6. Validate.
    report = validate_all(materials_by_appendix)
    if not report.ok:
        logger.error(
            "Validation failed: %d errors, %d warnings. See data/validation_report.json.",
            report.error_count, report.warning_count,
        )
        # Still write outputs so users can inspect.
    else:
        logger.info(
            "Validation passed: %d errors, %d warnings.",
            report.error_count, report.warning_count,
        )

    # 7. Write outputs.
    out_dir = Path(settings.output_dir)
    _write_json(out_dir / "appendix_a.json",
                {"appendix": "A", "materials": materials_by_appendix.get("A", [])})
    _write_json(out_dir / "appendix_b.json",
                {"appendix": "B", "materials": materials_by_appendix.get("B", [])})
    _write_json(out_dir / "appendix_c.json",
                {"appendix": "C", "materials": materials_by_appendix.get("C", [])})
    _write_json(out_dir / "metadata.json", decision.current.to_dict())
    _write_json(out_dir / "validation_report.json", report.to_dict())

    # 8. Decide exit code.
    if not report.ok and settings.fail_on_validation:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="nfrc101-sync",
        description="Sync NFRC 101 material libraries from the published PDF.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Reprocess even if the PDF has not changed.",
    )
    parser.add_argument(
        "--no-fail", action="store_true",
        help="Do not exit non-zero on validation errors (still writes report).",
    )
    parser.add_argument(
        "--log-level", default=None,
        help="Override logging level (e.g. DEBUG, INFO, WARNING).",
    )
    args = parser.parse_args(argv)

    settings = config.Settings.from_env()
    # Apply CLI overrides by rebuilding the frozen dataclass. Use dataclasses.replace
    # so we never drop a field by accident when adding new settings later.
    from dataclasses import replace
    if args.log_level:
        settings = replace(settings, log_level=args.log_level)
    if args.no_fail:
        settings = replace(settings, fail_on_validation=False)

    return run_pipeline(settings, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
