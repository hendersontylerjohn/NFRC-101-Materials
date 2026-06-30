"""Update monitor: decides whether the NFRC 101 PDF has changed.

Strategy (in priority order):
  1. SHA256 of the downloaded PDF bytes (primary, content-addressed).
  2. PDF URL comparison (secondary, catches moves before content changes).

State is persisted in `data/metadata.json`. When no change is detected the
caller should exit successfully WITHOUT creating a commit.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .pdf_downloader import DownloadResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MetadataRecord:
    """On-disk record of the last processed NFRC 101 PDF.

    Stored as `data/metadata.json`. Used to detect changes between runs.
    """

    source_url: str
    pdf_url: str
    pdf_hash: str
    document_version: str
    last_checked: str  # ISO-8601 UTC

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MetadataRecord":
        return cls(
            source_url=str(d.get("source_url", "")),
            pdf_url=str(d.get("pdf_url", "")),
            pdf_hash=str(d.get("pdf_hash", "")),
            document_version=str(d.get("document_version", "")),
            last_checked=str(d.get("last_checked", "")),
        )

    @classmethod
    def empty(cls, source_url: str = "") -> "MetadataRecord":
        return cls(
            source_url=source_url,
            pdf_url="",
            pdf_hash="",
            document_version="",
            last_checked="",
        )


@dataclass(frozen=True)
class ChangeDecision:
    """Outcome of a monitoring check.

    `changed` is True iff the pipeline should reprocess all appendices.
    """

    changed: bool
    reason: str
    current: MetadataRecord
    previous: MetadataRecord | None


def _now_iso() -> str:
    """Current UTC time in ISO-8601 with 'Z' suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _extract_version(pdf_url: str) -> str:
    """Best-effort extraction of a document version label from the PDF URL.

    Example: 'NFRC_101-2026_E0A2.pdf' -> 'NFRC 101-2026 E0A2'
    """
    import re
    base = Path(pdf_url).name
    # Strip extension.
    stem = base.rsplit(".", 1)[0] if "." in base else base
    # Normalize separators.
    stem = stem.replace("_", " ").replace("-", " ")
    # Collapse whitespace.
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem


def load_metadata(path: Path) -> MetadataRecord | None:
    """Load the persisted metadata record, or None if not present/invalid."""
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return MetadataRecord.from_dict(data)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning("Could not load metadata from %s: %s", path, e)
        return None


def save_metadata(path: Path, record: MetadataRecord) -> None:
    """Persist the metadata record to disk atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record.to_dict(), f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    tmp.replace(path)
    logger.info("Metadata written to %s", path)


def decide(
    download: DownloadResult,
    source_url: str,
    previous: MetadataRecord | None,
) -> ChangeDecision:
    """Decide whether the pipeline should reprocess.

    Rules (any one triggers a reprocess):
      * No previous record.
      * SHA256 hash differs.
      * PDF URL differs.

    Returns a ChangeDecision with the *current* MetadataRecord populated.
    """
    current = MetadataRecord(
        source_url=source_url,
        pdf_url=download.url,
        pdf_hash=download.sha256,
        document_version=_extract_version(download.url),
        last_checked=_now_iso(),
    )

    if previous is None:
        return ChangeDecision(
            changed=True,
            reason="no previous metadata record",
            current=current,
            previous=None,
        )

    if previous.pdf_hash != current.pdf_hash:
        return ChangeDecision(
            changed=True,
            reason=f"PDF hash changed: {previous.pdf_hash} -> {current.pdf_hash}",
            current=current,
            previous=previous,
        )

    if previous.pdf_url != current.pdf_url:
        return ChangeDecision(
            changed=True,
            reason=f"PDF URL changed: {previous.pdf_url} -> {current.pdf_url}",
            current=current,
            previous=previous,
        )

    return ChangeDecision(
        changed=False,
        reason="PDF hash and URL unchanged",
        current=current,
        previous=previous,
    )


def metadata_path(settings: "config.Settings") -> Path:
    """Return the absolute path to metadata.json for the given settings."""
    return Path(settings.output_dir) / "metadata.json"
