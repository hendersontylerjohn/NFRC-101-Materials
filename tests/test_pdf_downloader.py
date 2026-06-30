"""Tests for src/pdf_downloader.py."""
from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.pdf_downloader import (
    DownloadResult,
    compute_sha256,
    discover_pdf_url,
    download_pdf,
)


def test_compute_sha256_matches_hashlib(tmp_path: Path) -> None:
    payload = b"hello world\n" * 100
    p = tmp_path / "f.bin"
    p.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert compute_sha256(p) == expected


def test_compute_sha256_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.bin"
    p.write_bytes(b"")
    assert compute_sha256(p) == hashlib.sha256(b"").hexdigest()


def test_download_pdf_writes_file_and_returns_sha256(tmp_path: Path) -> None:
    payload = b"%PDF-1.4 fake pdf bytes for testing"
    expected_sha = hashlib.sha256(payload).hexdigest()

    # Build a fake `requests.Session.get` context-manager response.
    fake_response = MagicMock()
    fake_response.__enter__.return_value = fake_response
    fake_response.__exit__.return_value = False
    fake_response.raise_for_status.return_value = None
    fake_response.headers = {"Content-Type": "application/pdf"}
    fake_response.url = "https://example.com/test.pdf"
    fake_response.iter_content = lambda chunk_size: [payload]

    fake_session = MagicMock()
    fake_session.get.return_value = fake_response

    with patch("src.pdf_downloader._session_with_retries", return_value=fake_session):
        result = download_pdf(
            "https://example.com/test.pdf",
            dest_dir=tmp_path,
            timeout=5,
            retries=1,
        )

    assert isinstance(result, DownloadResult)
    assert result.sha256 == expected_sha
    assert result.size_bytes == len(payload)
    assert result.url == "https://example.com/test.pdf"
    assert result.content_type == "application/pdf"
    assert result.local_path.exists()
    assert result.local_path.read_bytes() == payload


def test_download_pdf_retries_on_failure_then_succeeds(tmp_path: Path) -> None:
    payload = b"%PDF-1.4 fake pdf"
    expected_sha = hashlib.sha256(payload).hexdigest()

    good_response = MagicMock()
    good_response.__enter__.return_value = good_response
    good_response.__exit__.return_value = False
    good_response.raise_for_status.return_value = None
    good_response.headers = {"Content-Type": "application/pdf"}
    good_response.url = "https://example.com/test.pdf"
    good_response.iter_content = lambda chunk_size: [payload]

    import requests
    bad_response = MagicMock()
    bad_response.__enter__.side_effect = requests.ConnectionError("boom")
    bad_response.__exit__.return_value = False

    fake_session = MagicMock()
    fake_session.get.side_effect = [bad_response, good_response]

    with patch("src.pdf_downloader._session_with_retries", return_value=fake_session), \
         patch("time.sleep", return_value=None):
        result = download_pdf(
            "https://example.com/test.pdf",
            dest_dir=tmp_path,
            timeout=5,
            retries=3,
        )
    assert result.sha256 == expected_sha
    assert fake_session.get.call_count == 2


def test_download_pdf_raises_after_all_retries_fail(tmp_path: Path) -> None:
    import requests
    from src.pdf_downloader import DownloadError

    bad_response = MagicMock()
    bad_response.__enter__.side_effect = requests.ConnectionError("boom")
    bad_response.__exit__.return_value = False

    fake_session = MagicMock()
    fake_session.get.return_value = bad_response

    with patch("src.pdf_downloader._session_with_retries", return_value=fake_session), \
         patch("time.sleep", return_value=None), \
         pytest.raises(DownloadError):
        download_pdf(
            "https://example.com/test.pdf",
            dest_dir=tmp_path,
            timeout=5,
            retries=2,
        )


def test_discover_pdf_url_finds_nfrc_link() -> None:
    html = """
    <html><body>
      <p>See the latest <a href="https://cdn.example.com/2026/NFRC_101-2026_E0A2.pdf">NFRC 101 PDF</a></p>
    </body></html>
    """
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.text = html
    fake_response.url = "https://nfrccommunity.org/page/TD"

    fake_session = MagicMock()
    fake_session.get.return_value = fake_response

    with patch("src.pdf_downloader._session_with_retries", return_value=fake_session):
        url = discover_pdf_url("https://nfrccommunity.org/page/TD", timeout=5, retries=1)
    assert url == "https://cdn.example.com/2026/NFRC_101-2026_E0A2.pdf"


def test_discover_pdf_url_falls_back_to_any_pdf_link() -> None:
    html = '<a href="/docs/manual.pdf">Manual</a>'
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.text = html
    fake_response.url = "https://example.com/landing"
    fake_session = MagicMock()
    fake_session.get.return_value = fake_response
    with patch("src.pdf_downloader._session_with_retries", return_value=fake_session):
        url = discover_pdf_url("https://example.com/landing", timeout=5, retries=1)
    assert url == "https://example.com/docs/manual.pdf"


def test_discover_pdf_url_raises_when_no_link_found() -> None:
    from src.pdf_downloader import DownloadError
    html = "<html><body><p>No PDFs here.</p></body></html>"
    fake_response = MagicMock()
    fake_response.raise_for_status.return_value = None
    fake_response.text = html
    fake_response.url = "https://example.com/landing"
    fake_session = MagicMock()
    fake_session.get.return_value = fake_response
    with patch("src.pdf_downloader._session_with_retries", return_value=fake_session), \
         patch("time.sleep", return_value=None), \
         pytest.raises(DownloadError):
        discover_pdf_url("https://example.com/landing", timeout=5, retries=1)
