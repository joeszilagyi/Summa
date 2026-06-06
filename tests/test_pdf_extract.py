from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from tools.collateral import pdf_extract


def test_get_page_count_reports_timeout_warning(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd=["pdfinfo", str(pdf_path)], timeout=pdf_extract.PDFINFO_TIMEOUT_SECONDS)

    monkeypatch.setattr(pdf_extract.subprocess, "run", fake_run)

    warnings: list[str] = []
    assert pdf_extract.get_page_count(pdf_path, warnings) == 0
    assert warnings == [f"pdfinfo timed out after {pdf_extract.PDFINFO_TIMEOUT_SECONDS}s"]


def test_extract_pdf_metadata_reads_pypdf_metadata(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    fake_reader = SimpleNamespace(
        metadata={
            "/Title": "Sample Title",
            "/Author": "Ada Lovelace",
            "/Subject": "Testing",
            "/Creator": "Fixture Creator",
            "/Producer": "Fixture Producer",
            "/CreationDate": "D:20240603123456Z",
            "/ModDate": "D:20240604123456Z",
        }
    )
    fake_module = SimpleNamespace(PdfReader=lambda _fh: fake_reader)
    monkeypatch.setitem(sys.modules, "pypdf", fake_module)

    assert pdf_extract.extract_pdf_metadata(pdf_path) == {
        "title": "Sample Title",
        "author": "Ada Lovelace",
        "subject": "Testing",
        "creator": "Fixture Creator",
        "producer": "Fixture Producer",
        "creation_date": "D:20240603123456Z",
        "modification_date": "D:20240604123456Z",
    }


def test_extract_text_layer_path_writes_canonical_sidecars(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    written: dict[str, object] = {}

    def fake_get_page_count(_pdf_path: Path, warnings: list[str] | None = None) -> int:
        if warnings is not None:
            warnings.append("pdfinfo ok")
        return 2

    monkeypatch.setattr(pdf_extract, "get_page_count", fake_get_page_count)
    monkeypatch.setattr(pdf_extract, "detect_text_layer", lambda _pdf_path, _page_count: (True, "Line 1\nLine 2\n"))
    monkeypatch.setattr(
        pdf_extract,
        "extract_pdf_metadata",
        lambda _pdf_path: {"title": "Sample Title", "author": "Ada Lovelace"},
    )
    monkeypatch.setattr(pdf_extract, "sha256_file", lambda _path: "sha256:file-hash")
    monkeypatch.setattr(pdf_extract, "sha256_text", lambda text: f"sha256:text:{text}")
    monkeypatch.setattr(pdf_extract, "_now_iso", lambda: "2026-06-03T12:34:56Z")

    def fake_write_text(path: Path, text: str) -> None:
        written["text_path"] = path
        written["text"] = text

    def fake_write_json(path: Path, payload: dict) -> None:
        written["meta_path"] = path
        written["meta"] = payload

    monkeypatch.setattr(pdf_extract, "_write_atomic_text", fake_write_text)
    monkeypatch.setattr(pdf_extract, "_write_atomic_json", fake_write_json)

    assert pdf_extract.extract(pdf_path, force=False, lang=None, dry_run=False) == 0

    assert written["text_path"] == pdf_path.with_suffix(".txt")
    assert written["text"] == "Line 1\nLine 2"
    meta = written["meta"]
    assert meta["source_pdf"] == str(pdf_path)
    assert meta["extraction_method"] == "text_layer"
    assert meta["ocr_language"] is None
    assert meta["page_count"] == 2
    assert meta["char_count"] == len("Line 1\nLine 2")
    assert meta["warnings"] == ["pdfinfo ok"]
    assert meta["pdf_metadata"] == {"title": "Sample Title", "author": "Ada Lovelace"}
    assert meta["capture_event"]["content_hash_sha256"] == "sha256:file-hash"
    assert meta["extraction_record"]["normalized_text_hash_sha256"] == "sha256:text:Line 1\nLine 2"


def test_extract_falls_back_to_ocr_and_records_warnings(monkeypatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    written: dict[str, object] = {}

    monkeypatch.setattr(pdf_extract, "get_page_count", lambda _pdf_path, warnings=None: 1)
    monkeypatch.setattr(pdf_extract, "detect_text_layer", lambda _pdf_path, _page_count: (False, ""))
    monkeypatch.setattr(
        pdf_extract,
        "extract_via_ocr",
        lambda _pdf_path, lang, page_count: ("OCR output\n", [f"rendered {page_count} pages with {lang}"]),
    )
    monkeypatch.setattr(pdf_extract, "extract_pdf_metadata", lambda _pdf_path: {})
    monkeypatch.setattr(pdf_extract, "infer_ocr_lang", lambda _pdf_path: "eng")
    monkeypatch.setattr(pdf_extract, "sha256_file", lambda _path: "sha256:file-hash")
    monkeypatch.setattr(pdf_extract, "sha256_text", lambda text: f"sha256:text:{text}")
    monkeypatch.setattr(pdf_extract, "_now_iso", lambda: "2026-06-03T12:34:56Z")

    def fake_write_text(path: Path, text: str) -> None:
        written["text_path"] = path
        written["text"] = text

    def fake_write_json(path: Path, payload: dict) -> None:
        written["meta_path"] = path
        written["meta"] = payload

    monkeypatch.setattr(pdf_extract, "_write_atomic_text", fake_write_text)
    monkeypatch.setattr(pdf_extract, "_write_atomic_json", fake_write_json)

    assert pdf_extract.extract(pdf_path, force=False, lang=None, dry_run=False) == 0

    assert written["text"] == "OCR output"
    meta = written["meta"]
    assert meta["extraction_method"] == "ocr"
    assert meta["ocr_language"] == "eng"
    assert meta["warnings"] == ["rendered 1 pages with eng"]
    assert meta["extraction_record"]["normalized_text_hash_sha256"] == "sha256:text:OCR output"
