"""PDF downloader with SHA256 hashing and resilient retries.

This module is responsible ONLY for fetching the PDF bytes from a URL and
computing a content hash. It does not interpret the PDF.

Design goals:
  * Idempotent: re-running with the same URL returns the cached file.
  * Resilient: transient network errors are retried with exponential backoff.
  * Observable: structured logging at every step.
  * Pure: no side effects beyond the local filesystem cache.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DownloadResult:
    """Outcome of a successful download."""

    url: str           # The actual URL the bytes came from (after redirects).
    local_path: Path   # Where the bytes were stored on disk.
    sha256: str        # Lowercase hex SHA256 of the PDF bytes.
    size_bytes: int    # Number of bytes downloaded.
    content_type: str  # Value of the HTTP Content-Type header.


class DownloadError(RuntimeError):
    """Raised when the PDF cannot be fetched after all retries."""


def _session_with_retries(retries: int) -> requests.Session:
    """Create a requests Session with retry-friendly defaults.

    We use a realistic browser User-Agent and common browser headers because
    the NFRC community site (Higher Logic / YMaws) returns 403 to plain
    bot-style User-Agents. The actual PDF on cdn.ymaws.com does not require
    this, but the landing page on nfrccommunity.org does.
    """
    import requests.adapters
    from urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            # Browser-like UA helps bypass naive bot-protection on the
            # landing page. The CDN that serves the PDF itself is permissive.
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/pdf,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": "1",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
    )
    return session


_PDF_LINK_RE = re.compile(
    r'href\s*=\s*["\']([^"\']*?NFRC[_-]?101[^"\']*?\.pdf)["\']',
    re.IGNORECASE,
)


def discover_pdf_url(landing_page_url: str, *, timeout: int = 60, retries: int = 3) -> str:
    """Scrape the NFRC landing page to discover the current NFRC 101 PDF URL.

    The NFRC community publishes a 'Technical Documents' page that links to the
    most recent NFRC 101 revision. We follow that page and look for a hyperlink
    whose path contains 'NFRC_101' (case-insensitive) and ends in '.pdf'.

    Raises:
        DownloadError: if the page cannot be fetched or no PDF link is found.
    """
    session = _session_with_retries(retries)
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            logger.info("Discovering PDF URL from landing page (attempt %d/%d): %s",
                        attempt, retries, landing_page_url)
            r = session.get(landing_page_url, timeout=timeout, allow_redirects=True)
            r.raise_for_status()
            html = r.text
            m = _PDF_LINK_RE.search(html)
            if not m:
                # Some sites emit relative URLs or use different attribute orders.
                # Fallback: find any .pdf link.
                fallback = re.search(r'href\s*=\s*["\']([^"\']*\.pdf)["\']', html, re.IGNORECASE)
                if not fallback:
                    raise DownloadError(
                        f"No NFRC 101 PDF link found on landing page: {landing_page_url}"
                    )
                raw_link = fallback.group(1)
            else:
                raw_link = m.group(1)
            # Resolve relative URLs against the final URL (after redirects).
            base = r.url
            absolute = urljoin(base, raw_link)
            logger.info("Discovered PDF URL: %s", absolute)
            return absolute
        except (requests.RequestException, DownloadError) as e:
            last_err = e
            wait = 1.5 ** attempt
            logger.warning("Discovery attempt %d failed: %s (retrying in %.1fs)",
                           attempt, e, wait)
            time.sleep(wait)
    raise DownloadError(
        f"Failed to discover PDF URL after {retries} attempts: {last_err}"
    ) from last_err


def download_pdf(
    url: str,
    dest_dir: Path,
    *,
    timeout: int = 60,
    retries: int = 3,
    filename: str | None = None,
) -> DownloadResult:
    """Download a PDF from `url` into `dest_dir` and return a DownloadResult.

    The file is named after the URL's basename, or `filename` if provided.
    The destination directory is created if it does not exist.

    Raises:
        DownloadError: if the download fails after all retries.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        # Use last path segment; fall back to 'nfrc101.pdf' if empty.
        path = urlparse(url).path
        filename = Path(path).name or "nfrc101.pdf"
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"

    dest_path = dest_dir / filename
    session = _session_with_retries(retries)
    last_err: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            logger.info("Downloading PDF (attempt %d/%d): %s", attempt, retries, url)
            with session.get(url, timeout=timeout, allow_redirects=True, stream=True) as r:
                r.raise_for_status()
                content_type = r.headers.get("Content-Type", "application/octet-stream")
                # Stream to disk so we don't load the whole PDF into memory.
                tmp_path = dest_path.with_suffix(dest_path.suffix + ".part")
                h = hashlib.sha256()
                size = 0
                with open(tmp_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        h.update(chunk)
                        size += len(chunk)
                tmp_path.replace(dest_path)
                sha = h.hexdigest()
                logger.info(
                    "Downloaded %d bytes, sha256=%s -> %s",
                    size, sha, dest_path,
                )
                return DownloadResult(
                    url=r.url,
                    local_path=dest_path,
                    sha256=sha,
                    size_bytes=size,
                    content_type=content_type,
                )
        except (requests.RequestException, OSError) as e:
            last_err = e
            wait = 1.5 ** attempt
            logger.warning("Download attempt %d failed: %s (retrying in %.1fs)",
                           attempt, e, wait)
            time.sleep(wait)

    raise DownloadError(
        f"Failed to download {url} after {retries} attempts: {last_err}"
    ) from last_err


def compute_sha256(path: Path) -> str:
    """Compute the SHA256 hex digest of an existing file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
