# NFRC 101 Materials Library Sync

Automated synchronization of [NFRC 101](https://nfrccommunity.org/page/TD) material
properties into machine-readable JSON. The pipeline monitors the published NFRC 101
PDF for revisions, extracts Appendices A, B, and C, parses every material record
into a structured schema, validates the result, and commits the updated JSON to
this repository via GitHub Actions.

JSON is the source of truth. The architecture permits a future XML export via a
thin adapter, but XML is intentionally not part of the primary implementation.

---

## What gets produced

```text
data/
├── appendix_a.json          # Generic thermophysical properties (basic set)
├── appendix_b.json          # Generic thermophysical properties (extended set)
├── appendix_c.json          # Proprietary thermophysical properties
├── metadata.json            # Source URL, SHA-256, document version, last-checked
└── validation_report.json   # Counts, errors, warnings, per-issue detail
```

Each appendix JSON file has the shape:

```json
{
  "appendix": "A",
  "materials": [ /* … */ ]
}
```

`metadata.json` has the shape:

```json
{
  "source_url": "https://nfrccommunity.org/page/TD",
  "pdf_url":    "https://cdn.ymaws.com/.../NFRC_101-2026_E0A2.pdf",
  "pdf_hash":   "274321c26f2f3b825cfb2d4b2676a6315a6992b6b4f256cca1bbe9c4bd6e1925",
  "document_version": "NFRC 101 2026 E0A2",
  "last_checked": "2026-06-30T05:00:57Z"
}
```

---

## Repository layout

```text
.github/workflows/
    update.yml                 # Scheduled + manual + push-triggered pipeline

src/
    __init__.py
    config.py                  # All tunable settings (env vars + dataclass)
    pdf_downloader.py          # Download + SHA-256 + landing-page discovery
    monitor.py                 # Change detection (hash + URL), metadata.json I/O
    appendix_locator.py        # Heading-based appendix detection (no fixed pages)
    table_extractor.py         # pdfplumber-based table extraction + merging
    material_parser.py         # Row → structured material dict
    validator.py               # Duplicates, required fields, numerics, dates, ranges
    main.py                    # CLI entry point / orchestrator

tests/
    conftest.py                # Synthetic PDF fixtures
    test_pdf_downloader.py
    test_monitor.py
    test_appendix_locator.py
    test_table_extractor.py
    test_material_parser.py
    test_validator.py

data/                          # Generated JSON output (committed)
requirements.txt               # Runtime dependencies
requirements-dev.txt           # Runtime + test dependencies
```

---

## Data model

### Appendix A & B (generic materials)

| Field                            | Type        | Notes                                                |
| -------------------------------- | ----------- | ---------------------------------------------------- |
| `material_name`                  | string      | Required.                                            |
| `conductivity_wmk`               | float\|null | W/m·K (primary SI unit).                            |
| `conductivity_btu_hr_ft_f`       | float\|null | Btu/hr·ft·°F.                                        |
| `conductivity_btu_in_hr_ft2_f`   | float\|null | Btu·in/hr·ft²·°F.                                    |
| `source_ref`                     | string      | Reference list (e.g. `"1,3,15"`).                    |
| `emissivity`                     | float\|null | ε. `null` when source says `"-"` (default 0.9).      |
| `source_appendix`                | `"A"`\|`"B"`| Required.                                            |
| `category`                       | string      | Optional section header (e.g. `"Elastomers"`).       |
| `notes`                          | object      | Optional. Preserves `"See Appendix A"`, units, etc.  |

### Appendix C (proprietary materials)

| Field                            | Type        | Notes                                                |
| -------------------------------- | ----------- | ---------------------------------------------------- |
| `participant`                    | string      | Required. Manufacturer / submitter.                  |
| `product`                        | string      | Required. Product name.                              |
| `density_kgm3`                   | float\|null | kg/m³. May carry `density_note` for annotations.     |
| `conductivity_wmk`               | float\|null | W/m·K.                                               |
| `conductivity_btu_hr_ft_f`       | float\|null | Btu/hr·ft·°F.                                        |
| `conductivity_btu_in_hr_ft2_f`   | float\|null | Btu·in/hr·ft²·°F.                                    |
| `emissivity`                     | float\|null | ε. `null` when source says `"-"`.                    |
| `expiration_date`                | string\|null| ISO-8601 `YYYY-MM-DD`.                               |
| `source_appendix`                | `"C"`       | Required.                                            |
| `expiration_date_raw`            | string      | Optional. Original text when date could not be parsed. |
| `density_note`                   | string      | Optional. Annotation extracted from density cell.    |
| `notes`                          | object      | Optional. Preserves `"See Appendix A"`, units, etc.  |

---

## How update detection works

Two signals, in priority order:

1. **SHA-256 hash of the downloaded PDF bytes** (primary, content-addressed).
2. **PDF URL comparison** (secondary; catches moves before content changes).

State is persisted in `data/metadata.json`. When no change is detected, the
pipeline exits successfully **without** creating a commit. When the PDF has
changed, all appendices are reprocessed from scratch.

---

## How appendix detection works

The locator does NOT use fixed page numbers. For every page it walks every text
span and looks for lines matching `^APPENDIX\s+([A-Z])\b`. To qualify as a real
heading, the line must also have at least one span that is **bold** and **≥13.5pt**
— the NFRC 101 actual appendix headings are 14.52pt–18pt, while TOC entries and
body-text mentions are 12pt. The first qualifying page for each letter is its
start; the end is the start of the next appendix minus 1, or the last page of
the document for the final appendix.

This tolerates future page-count changes, additional appendices, removed
appendices, and minor font variations because the predicate is based on relative
prominence rather than absolute coordinates.

---

## How table extraction tolerates mess

The extractor is built around pdfplumber's `find_tables()` plus a normalization
layer:

* **Multi-page tables** — Header rows are detected by strict keyword matching
  (`name + conductivity + emissivity` for A/B; `participant + product +
  conductivity` for C). Tables on later pages whose header matches are merged
  into the same logical table; repeated headers are dropped.
* **Wrapped rows** — If a row has an empty first cell but content in other
  cells, and a previous row exists with a non-empty first cell, the second row
  is treated as a continuation and merged into the previous row.
* **Section headers** — Rows where only the first cell is populated (e.g.
  `"Elastomers"`, `"Polymers"`) are detected and used to annotate subsequent
  materials with a `category` field.
* **Unrelated tables filtered out** — Appendix A's page 51 contains a separate
  gas-coefficient table (`k = a + Bt + Ct²`) that shares the word
  `"conductivity"` with the main material table. The strict header check
  requires `"name"` AND `"emissivity"` too, so the gas table is correctly
  rejected. The gas-coefficient data is intentionally out of scope; if needed,
  it can be added as a future `appendix_a_gases.json`.
* **Engineering notation** — Values like `2.873x10-3` parse to `2.873e-3`.
* **Annotated numerics** — `6.92 (at 1.25")` parses to `6.92` with the
  annotation preserved in `density_note`.
* **`"See Appendix A"` cells** — Preserved verbatim in `notes.conductivity_wmk_note`
  with the numeric field set to `null`.

---

## How validation works

The validator runs five categories of checks after parsing:

1. **Duplicates** — Same `material_name` (A/B, case-insensitive) or same
   `participant + product` (C) appearing more than once.
2. **Missing required fields** — Per appendix: `material_name + source_appendix`
   for A/B; `participant + product + source_appendix` for C.
3. **Invalid numeric values** — Non-float types, `NaN`, `Infinity`.
4. **Invalid dates** — `expiration_date` must match `YYYY-MM-DD` AND be a real
   calendar date (so `2026-02-31` is rejected).
5. **Sanity ranges** — WARN-level: conductivity in `[0, 500]` W/m·K,
   emissivity in `[0, 1]`, density in `[0, 25 000]` kg/m³.

Categories 1–4 produce `ERROR`s; the workflow fails if any error is found
(unless `--no-fail`). Category 5 produces `WARNING`s by default and is
promoted to `ERROR`s only when `strict_sanity=True` (off by default).

---

## Quick start

### Run locally

```bash
# 1. Clone and install.
git clone <your-repo-url>
cd <repo>
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

# 2. Run the tests (synthetic fixtures only, no network).
pytest -q

# 3. Run the pipeline against the live NFRC 101 PDF.
python -m src.main

# 4. Inspect the output.
ls data/
cat data/metadata.json
cat data/validation_report.json
```

### Force reprocessing

```bash
python -m src.main --force
```

### Don't fail on validation errors

```bash
python -m src.main --no-fail
```

---

## Configuration

All runtime behavior is configurable via environment variables. None of these
are required for the default setup — the pipeline ships with sensible defaults.

| Variable                    | Default                                            | Purpose                                             |
| --------------------------- | -------------------------------------------------- | --------------------------------------------------- |
| `NFRC_LANDING_PAGE_URL`     | `https://nfrccommunity.org/page/TD`                | Page scraped to discover the current PDF URL.       |
| `NFRC_PDF_URL`              | _(unset)_                                          | Override; skip discovery and use this URL directly. |
| `NFRC_FALLBACK_PDF_URL`     | `https://cdn.ymaws.com/.../NFRC_101-2026_E0A2.pdf` | Used when discovery is blocked (WAF 403).           |
| `NFRC_OUTPUT_DIR`           | `./data`                                           | Where JSON files are written.                       |
| `NFRC_PDF_CACHE_DIR`        | `~/.nfrc101/cache`                                 | Where downloaded PDFs are cached.                   |
| `NFRC_HTTP_TIMEOUT`         | `60`                                               | Per-request timeout in seconds.                     |
| `NFRC_HTTP_RETRIES`         | `3`                                                | Number of retry attempts on network errors.         |
| `NFRC_LOG_LEVEL`            | `INFO`                                             | Logging level.                                      |
| `NFRC_FAIL_ON_VALIDATION`   | `true`                                             | Whether validation errors exit non-zero.            |

### About the fallback URL

The NFRC community site (`nfrccommunity.org`) is fronted by a WAF that returns
HTTP 403 to many cloud/CI IP ranges. The CDN that serves the actual PDF
(`cdn.ymaws.com`) is permissive. The pipeline tries discovery first; if that
fails, it falls back to the last-known-good direct URL.

When NFRC publishes a new revision:

1. If discovery works in your environment, the pipeline picks up the new URL
   automatically. No action needed.
2. If discovery is blocked, update `DEFAULT_FALLBACK_PDF_URL` in
   `src/config.py` (or set `NFRC_FALLBACK_PDF_URL` as a GitHub Actions
   variable) and submit a PR.

---

## GitHub Actions workflow

The workflow in `.github/workflows/update.yml`:

* **Runs daily at 06:00 UTC** (`schedule: cron: '0 6 * * *'`).
* **Supports manual runs** (`workflow_dispatch`) with an optional `force` input.
* **Re-runs on push to `main`** when pipeline source files change, so we catch
  regressions in the workflow itself.
* Uses `concurrency` to cancel superseded runs.
* Requires only the default `GITHUB_TOKEN` with `contents: write` permission.
* Installs dependencies, runs the test suite, runs the pipeline, and **commits
  `data/` only when files have actually changed** (idempotent).

Commit messages include the document version and a short SHA prefix, e.g.:

```text
chore(data): sync NFRC 101 materials (NFRC 101 2026 E0A2, sha 274321c26f2f)
```

---

## Architecture decisions

### Why JSON and not XML?

JSON is the source of truth because:

* Native support in Python, JavaScript, and every modern language.
* Schema validation tooling is mature (`jsonschema`, etc.).
* Diffs are reviewable on GitHub.

A future XML export can be added as a thin adapter that walks the JSON tree and
emits tags — no parser changes needed.

### Why PyMuPDF AND pdfplumber?

PyMuPDF (`fitz`) is used for the **initial heading scan** because it exposes
per-span font metadata (name, size, flags) at high speed. pdfplumber is used
for **table extraction** because its `find_tables()` does the heavy lifting of
detecting cell boundaries from visible lines and rectangles. Each library does
one job well.

### Why a "strict" header match?

The NFRC 101 PDF has multiple tables that share keywords with the target
appendix tables. The most important case is Appendix A's page 51, which
contains a gas-coefficient table (`k = a + Bt + Ct²`). Its header includes the
word `"conductivity"`, so a loose OR-match would incorrectly include it. The
strict AND-match requires `"name"` AND `"emissivity"` too, which the gas table
lacks. This pattern generalizes to future revisions: any new table that doesn't
match ALL required keywords is excluded.

### Why the SHA-256 hash AND the URL?

The hash is content-addressed and the ultimate source of truth. The URL is a
secondary signal that catches moves before content changes (e.g. when NFRC
renames the file from `NFRC_101-2026_E0A2.pdf` to `NFRC_101-2026_E0A3.pdf` but
the bytes are identical because only the errata marker changed). Either signal
triggers a reprocess.

### Why commit only when files change?

Idempotency. Without this check, the daily workflow would create an empty
commit every day, polluting the git history. With this check, commits appear
only when NFRC actually publishes a new revision.

---

## Error handling

Every failure mode in the spec is handled explicitly:

| Failure              | Where handled                | Behavior                                              |
| -------------------- | ---------------------------- | ----------------------------------------------------- |
| Network failure      | `pdf_downloader.py`          | Retry with exponential backoff, then `DownloadError`. |
| Landing page blocked | `main._resolve_pdf_url`      | Fall back to `NFRC_FALLBACK_PDF_URL`.                 |
| Missing appendix     | `appendix_locator.require_targets` | Raise `AppendixLocatorError`, exit 1.            |
| Corrupt PDF          | `fitz.open` / `pdfplumber.open` | Caught in `main.run_pipeline`, exit 1.             |
| No tables found      | `table_extractor.extract_table_for_appendix` | Raise `TableExtractionError`, exit 1. |
| Parse failure (row)  | `material_parser`            | Logs DEBUG and skips the row; never raises.           |
| Validation failure   | `validator.validate_all`     | Writes `validation_report.json`; exits 1 unless `--no-fail`. |

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q                    # all tests
pytest tests/test_validator.py -q
pytest --cov=src --cov-report=term-missing
```

The test suite uses **synthetic PDFs** generated by PyMuPDF (no network, no
fixtures to download) plus mocked HTTP for the downloader. Tests cover:

* Cell normalization, wrapped-row merging, repeated-header dropping.
* Heading detection (real heading vs. TOC vs. body-text mention).
* Engineering-number parsing (`2.873x10-3` → `2.873e-3`).
* Date normalization (`12/31/2029` → `2029-12-31`).
* All five validation categories.
* Change-detection logic (first run, hash change, URL change, no-change).
* HTTP retry behavior with mocked sessions.

---

## Future work

The architecture deliberately leaves room for:

* **XML export** — a `src/xml_exporter.py` adapter that walks the JSON tree.
* **Gas-coefficient data** — Appendix A's page 51 polynomial coefficients as a
  separate `data/appendix_a_gases.json` (currently filtered out).
* **Schema validation** — adopt `jsonschema` and ship a `schema/*.json` family.
* **Diff reports** — generate a Markdown diff between revisions for the commit
  message body.
* **Notifications** — post a Slack/Discord message when materials change.

None of these are required for the primary implementation.
