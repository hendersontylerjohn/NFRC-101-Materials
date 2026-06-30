"""Tests for src/monitor.py."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.pdf_downloader import DownloadResult
from src.monitor import (
    ChangeDecision,
    MetadataRecord,
    _extract_version,
    decide,
    load_metadata,
    save_metadata,
)


def _make_download(url: str, sha: str, path: Path) -> DownloadResult:
    return DownloadResult(
        url=url,
        local_path=path,
        sha256=sha,
        size_bytes=1024,
        content_type="application/pdf",
    )


def test_extract_version_from_typical_nfrc_url() -> None:
    assert (
        _extract_version(
            "https://cdn.ymaws.com/nfrccommunity.org/resource/resmgr/"
            "2026technicaldocs/NFRC_101-2026_E0A2.pdf"
        )
        == "NFRC 101 2026 E0A2"
    )


def test_extract_version_handles_simple_filename() -> None:
    assert _extract_version("https://example.com/doc.pdf") == "doc"


def test_metadata_round_trip(tmp_path: Path) -> None:
    rec = MetadataRecord(
        source_url="https://nfrccommunity.org/page/TD",
        pdf_url="https://cdn.example.com/NFRC_101-2026_E0A2.pdf",
        pdf_hash="abc123",
        document_version="NFRC 101 2026 E0A2",
        last_checked="2026-06-30T12:00:00Z",
    )
    p = tmp_path / "metadata.json"
    save_metadata(p, rec)
    loaded = load_metadata(p)
    assert loaded is not None
    assert loaded == rec


def test_load_metadata_missing_file_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "does_not_exist.json"
    assert load_metadata(p) is None


def test_load_metadata_invalid_json_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "metadata.json"
    p.write_text("{not valid json")
    assert load_metadata(p) is None


def test_decide_first_run_is_change(tmp_path: Path) -> None:
    dl = _make_download("https://example.com/a.pdf", "sha-aaa", tmp_path / "a.pdf")
    decision = decide(dl, source_url="https://example.com/landing", previous=None)
    assert decision.changed is True
    assert "no previous" in decision.reason.lower()
    assert decision.previous is None
    assert decision.current.pdf_hash == "sha-aaa"
    assert decision.current.source_url == "https://example.com/landing"


def test_decide_hash_change_triggers_reprocess(tmp_path: Path) -> None:
    prev = MetadataRecord(
        source_url="https://example.com/landing",
        pdf_url="https://example.com/a.pdf",
        pdf_hash="sha-old",
        document_version="v1",
        last_checked="2026-01-01T00:00:00Z",
    )
    dl = _make_download("https://example.com/a.pdf", "sha-new", tmp_path / "a.pdf")
    decision = decide(dl, source_url="https://example.com/landing", previous=prev)
    assert decision.changed is True
    assert "hash" in decision.reason.lower()


def test_decide_url_change_triggers_reprocess(tmp_path: Path) -> None:
    prev = MetadataRecord(
        source_url="https://example.com/landing",
        pdf_url="https://example.com/old.pdf",
        pdf_hash="sha-same",
        document_version="v1",
        last_checked="2026-01-01T00:00:00Z",
    )
    dl = _make_download("https://example.com/new.pdf", "sha-same", tmp_path / "new.pdf")
    decision = decide(dl, source_url="https://example.com/landing", previous=prev)
    assert decision.changed is True
    assert "url" in decision.reason.lower()


def test_decide_no_change_when_hash_and_url_match(tmp_path: Path) -> None:
    prev = MetadataRecord(
        source_url="https://example.com/landing",
        pdf_url="https://example.com/a.pdf",
        pdf_hash="sha-same",
        document_version="v1",
        last_checked="2026-01-01T00:00:00Z",
    )
    dl = _make_download("https://example.com/a.pdf", "sha-same", tmp_path / "a.pdf")
    decision = decide(dl, source_url="https://example.com/landing", previous=prev)
    assert decision.changed is False
    assert "unchanged" in decision.reason.lower()
