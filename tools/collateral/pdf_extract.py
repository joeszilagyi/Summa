#!/usr/bin/env python3
"""
PDF text and metadata extractor for the collateral intake pipeline.

Writes two sidecars beside the source PDF:
  <name>.txt       — temporary normalized extracted text for review/promotion
  <name>.meta.json — extraction provenance + retention/refetch metadata

Text-layer PDFs (most government docs) are handled by pdfminer.six.
Scanned/image PDFs are handled by pdf2image + tesseract (OCR).
Detection is automatic: if extracted chars-per-page < threshold, OCR kicks in.
When pdfinfo reports a page count, OCR renders one page at a time to bound memory.

Usage:
  pdf_extract.py <pdf-path> [--force] [--lang LANG] [--dry-run]

Operator entrypoint/docs:
  tools/scripts/collateral_extract.sh
  docs/scripts/collateral_extract.md

When changing CLI or sidecar behavior, keep the operator docs in sync.

Flags:
  --force       Re-extract even if sidecars already exist
  --lang LANG   Tesseract language code (default: inferred from path)
                Common codes: eng, spa, fra, por, deu, rus, jpn
  --dry-run     Show planned extraction actions without writing sidecars

Exit codes:
  0  success
  1  extraction error
  2  bad arguments
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

EXTRACTOR_VERSION = "1.1"

# Pages with fewer than this many chars are treated as image-only.
TEXT_CHARS_PER_PAGE_MIN = 80
PDFINFO_TIMEOUT_SECONDS = 30
OCR_DPI = 300
OCR_RENDER_TIMEOUT_SECONDS = 300
OCR_PAGE_TIMEOUT_SECONDS = 120

# Optional language-from-path table for operator-local configuration.
_LANG_PATH_MAP: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def infer_ocr_lang(pdf_path: Path) -> str:
    p = str(pdf_path).lower().replace("\\", "/")
    for fragment, lang in _LANG_PATH_MAP.items():
        if fragment in p:
            return lang
    return "eng"


def get_page_count(pdf_path: Path, warnings: list[str] | None = None) -> int:
    """Use poppler's pdfinfo for page count."""
    try:
        result = subprocess.run(
            ["pdfinfo", str(pdf_path)],
            capture_output=True,
            text=True,
            timeout=PDFINFO_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            detail = " ".join(result.stderr.split()) or f"exit status {result.returncode}"
            _append_warning(warnings, f"pdfinfo failed: {detail}")
            return 0
        for line in result.stdout.splitlines():
            if line.startswith("Pages:"):
                match = re.search(r"Pages:\s*(\d+)", line)
                if match:
                    return int(match.group(1))
        _append_warning(warnings, "pdfinfo output did not include a Pages line")
    except subprocess.TimeoutExpired:
        _append_warning(warnings, f"pdfinfo timed out after {PDFINFO_TIMEOUT_SECONDS}s")
    except OSError as exc:
        _append_warning(warnings, f"pdfinfo failed to start: {exc}")
    return 0


def _append_warning(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\f", "\n\n")           # form-feed → paragraph break
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)      # collapse excess blank lines
    text = re.sub(r"[ \t]+\n", "\n", text)      # trailing whitespace
    return text.strip()


def _relative_to_collateral(pdf_path: Path) -> str:
    parts = pdf_path.parts
    try:
        idx = parts.index("collateral")
        return str(Path(*parts[idx:]))
    except ValueError:
        return str(pdf_path)


# ---------------------------------------------------------------------------
# PDF metadata via pypdf
# ---------------------------------------------------------------------------

def extract_pdf_metadata(pdf_path: Path) -> dict:
    meta: dict = {}
    try:
        import pypdf
        with open(pdf_path, "rb") as fh:
            reader = pypdf.PdfReader(fh)
            info = reader.metadata or {}
            meta = {
                "title":             _str(info.get("/Title")),
                "author":            _str(info.get("/Author")),
                "subject":           _str(info.get("/Subject")),
                "creator":           _str(info.get("/Creator")),
                "producer":          _str(info.get("/Producer")),
                "creation_date":     _str(info.get("/CreationDate")),
                "modification_date": _str(info.get("/ModDate")),
            }
    except Exception as exc:
        meta["_error"] = str(exc)
    return meta


def _str(val: object) -> str:
    if val is None:
        return ""
    return str(val)


# ---------------------------------------------------------------------------
# Text extraction: pdfminer (text-layer PDFs)
# ---------------------------------------------------------------------------

def extract_text_layer(pdf_path: Path) -> str:
    from pdfminer.high_level import extract_text
    text = extract_text(str(pdf_path))
    return text or ""


def detect_text_layer(pdf_path: Path, page_count: int) -> tuple[bool, str]:
    try:
        sample = extract_text_layer(pdf_path)
        if len(sample.strip()) == 0:
            return False, ""
        if page_count <= 0:
            return len(sample.strip()) >= TEXT_CHARS_PER_PAGE_MIN, sample
        return (len(sample.strip()) / max(page_count, 1)) >= TEXT_CHARS_PER_PAGE_MIN, sample
    except Exception:
        return False, ""


# ---------------------------------------------------------------------------
# Text extraction: tesseract (scanned/image PDFs)
# ---------------------------------------------------------------------------

def extract_via_ocr(pdf_path: Path, lang: str, page_count: int = 0) -> tuple[str, list[str]]:
    warnings: list[str] = []
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError as exc:
        raise RuntimeError(
            f"OCR dependencies not available ({exc}). "
            "Run bash tools/scripts/setup-collateral-env.sh to install them."
        ) from exc

    print(f"  OCR: converting pages to images (dpi={OCR_DPI}, lang={lang})")
    parts: list[str] = []
    rendered_pages = 0
    for page_number, img in _iter_ocr_page_images(convert_from_path, pdf_path, page_count):
        rendered_pages += 1
        try:
            page_text = pytesseract.image_to_string(
                img,
                lang=lang,
                timeout=OCR_PAGE_TIMEOUT_SECONDS,
            )
        finally:
            close = getattr(img, "close", None)
            if callable(close):
                close()
        if not page_text.strip():
            warnings.append(f"page {page_number}: OCR returned no text")
        parts.append(page_text)
    if page_count > 0 and rendered_pages < page_count:
        warnings.append(f"OCR rendered {rendered_pages}/{page_count} pages")
    return "\n\f\n".join(parts), warnings


def _iter_ocr_page_images(convert_from_path, pdf_path: Path, page_count: int):
    if page_count <= 0:
        pages = convert_from_path(
            str(pdf_path),
            dpi=OCR_DPI,
            timeout=OCR_RENDER_TIMEOUT_SECONDS,
        )
        yield from enumerate(pages, 1)
        return

    for page_number in range(1, page_count + 1):
        pages = convert_from_path(
            str(pdf_path),
            dpi=OCR_DPI,
            timeout=OCR_RENDER_TIMEOUT_SECONDS,
            first_page=page_number,
            last_page=page_number,
        )
        if pages:
            yield page_number, pages[0]


# ---------------------------------------------------------------------------
# Main extraction entry point
# ---------------------------------------------------------------------------

def extract(pdf_path: Path, force: bool, lang: str | None, dry_run: bool) -> int:
    txt_path  = pdf_path.with_suffix(".txt")
    meta_path = pdf_path.with_suffix(".meta.json")

    if txt_path.exists() and meta_path.exists() and not force:
        print(f"skip: {pdf_path.name} (sidecars exist; use --force to re-extract)")
        return 0

    warnings: list[str] = []

    page_count = get_page_count(pdf_path, warnings)
    if page_count == 0:
        detail = warnings[-1] if warnings else "pdfinfo returned 0 pages"
        print(f"  warning: {detail}; proceeding anyway", file=sys.stderr)

    has_text, text_layer_sample = detect_text_layer(pdf_path, page_count)
    method   = "text_layer" if has_text else "ocr"

    effective_lang = lang if lang is not None else infer_ocr_lang(pdf_path)

    if dry_run:
        _print_dry_run_summary(
            pdf_path=pdf_path,
            txt_path=txt_path,
            meta_path=meta_path,
            page_count=page_count,
            method=method,
            force=force,
        )
        return 0

    print(f"extracting: {pdf_path}")

    try:
        if has_text:
            raw_text = text_layer_sample if text_layer_sample else extract_text_layer(pdf_path)
        else:
            print(f"  sparse text layer ({page_count} pages) → OCR")
            raw_text, ocr_warnings = extract_via_ocr(pdf_path, effective_lang, page_count)
            warnings.extend(ocr_warnings)
    except Exception as exc:
        print(f"  error: {exc}", file=sys.stderr)
        return 1

    text      = normalize_text(raw_text)
    pdf_meta  = extract_pdf_metadata(pdf_path)
    rel_path  = _relative_to_collateral(pdf_path)

    extracted_at = _now_iso()
    meta_doc = {
        "source_pdf":         rel_path,
        "extracted_at":       extracted_at,
        "extractor_version":  EXTRACTOR_VERSION,
        "extraction_method":  method,
        "ocr_language":       effective_lang if method == "ocr" else None,
        "page_count":         page_count,
        "char_count":         len(text),
        "pdf_metadata":       pdf_meta,
        "warnings":           warnings,
        "rights_posture":     "unknown_review_required",
        "source_access": {
            "original_locator": rel_path,
            "refetchability_status": "uncertain",
        },
        "capture_event": {
            "captured_at": extracted_at,
            "capture_method": "local_file",
            "content_hash_sha256": sha256_file(pdf_path),
            "byte_retention_status": "temporary_processing_input",
            "discard_reason": None,
            "discarded_at": None,
        },
        "extraction_record": {
            "full_text_retention_status": "temporary_processing_input",
            "normalized_text_hash_sha256": sha256_text(text),
            "summary_short": "",
            "summary_long": "",
            "highlights": [],
            "keywords": [],
            "detected_entities": [],
            "relationships": [],
            "claims": [],
            "quality_warnings": warnings,
            "confidence": None,
            "review_state": "unreviewed",
        },
    }

    _write_atomic_text(txt_path, text)
    _write_atomic_json(meta_path, meta_doc)

    print(
        f"  → {txt_path.name}  "
        f"({len(text):,} chars, {page_count} pages, method={method})"
    )
    return 0


def _write_atomic_bytes(path: Path, payload: bytes) -> None:
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_file.write(payload)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            tmp = temp_file.name
        os.replace(tmp, path)
    finally:
        if tmp is not None and Path(tmp).exists():
            Path(tmp).unlink()


def _write_atomic_text(path: Path, text: str) -> None:
    _write_atomic_bytes(path, text.encode("utf-8"))


def _write_atomic_json(path: Path, payload: dict) -> None:
    encoded = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    _write_atomic_bytes(path, encoded.encode("utf-8"))


def _print_dry_run_summary(
    pdf_path: Path,
    txt_path: Path,
    meta_path: Path,
    page_count: int,
    method: str,
    force: bool,
) -> None:
    print(f"dry-run: {pdf_path}")
    print(f"  pages: {page_count}")
    print(f"  extraction method: {method}")
    print("  sidecars:")
    print(f"    - {txt_path}")
    print(f"    - {meta_path}")
    if txt_path.exists() and meta_path.exists() and not force:
        print("  skip: sidecars exist and --force not set")
    elif txt_path.exists() or meta_path.exists():
        print("  update: one or more sidecars will be replaced")
    else:
        print("  create: sidecars will be generated")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("pdf", help="Path to the PDF file to extract")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-extract even if sidecars already exist",
    )
    parser.add_argument(
        "--lang",
        default=None,
        metavar="LANG",
        help="Tesseract language code for OCR (default: inferred from path)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned extraction actions without writing sidecars",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf).resolve()

    if not pdf_path.exists():
        print(f"error: file not found: {pdf_path}", file=sys.stderr)
        sys.exit(2)
    if pdf_path.suffix.lower() != ".pdf":
        print(f"error: not a PDF file: {pdf_path}", file=sys.stderr)
        sys.exit(2)

    sys.exit(extract(pdf_path, force=args.force, lang=args.lang, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
