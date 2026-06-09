from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.collateral import pdf_extract


class _FakeImage:
    def __init__(self, label: str) -> None:
        self.label = label
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_normalize_text_canonicalizes_whitespace() -> None:
    assert pdf_extract.normalize_text("\fLine 1\r\n\r\n\r\nLine 2 \t\n") == "Line 1\n\nLine 2"


def test_infer_ocr_lang_uses_path_map_and_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_extract, "_LANG_PATH_MAP", {"spanish": "spa", "french": "fra"})

    assert pdf_extract.infer_ocr_lang(Path("/tmp/spanish/report.pdf")) == "spa"
    assert pdf_extract.infer_ocr_lang(Path("/tmp/other/report.pdf")) == "eng"


def test_relative_to_collateral_handles_nested_and_outside_paths(tmp_path: Path) -> None:
    collateral_pdf = tmp_path / "workspace" / "collateral" / "nested" / "doc.pdf"
    collateral_pdf.parent.mkdir(parents=True)
    collateral_pdf.write_bytes(b"%PDF-1.4\n")
    outside_pdf = tmp_path / "workspace" / "doc.pdf"
    outside_pdf.write_bytes(b"%PDF-1.4\n")

    assert pdf_extract._relative_to_collateral(collateral_pdf) == str(
        Path("collateral") / "nested" / "doc.pdf"
    )
    assert pdf_extract._relative_to_collateral(outside_pdf) == str(outside_pdf)


def test_get_page_count_reports_success_missing_pages_and_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    def fake_run_success(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="Pages: 7\n", stderr="")

    def fake_run_missing_pages(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=0, stdout="Title: Example\n", stderr="")

    def fake_run_oserror(*_args: object, **_kwargs: object) -> None:
        raise OSError("pdfinfo missing")

    monkeypatch.setattr(pdf_extract.subprocess, "run", fake_run_success)
    warnings: list[str] = []
    assert pdf_extract.get_page_count(pdf_path, warnings) == 7
    assert warnings == []

    monkeypatch.setattr(pdf_extract.subprocess, "run", fake_run_missing_pages)
    warnings = []
    assert pdf_extract.get_page_count(pdf_path, warnings) == 0
    assert warnings == ["pdfinfo output did not include a Pages line"]

    monkeypatch.setattr(pdf_extract.subprocess, "run", fake_run_oserror)
    warnings = []
    assert pdf_extract.get_page_count(pdf_path, warnings) == 0
    assert warnings == ["pdfinfo failed to start: pdfinfo missing"]


def test_extract_pdf_metadata_records_reader_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    fake_module = SimpleNamespace(PdfReader=lambda _fh: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setitem(sys.modules, "pypdf", fake_module)

    meta = pdf_extract.extract_pdf_metadata(pdf_path)
    assert meta["_error"] == "boom"


def test_detect_text_layer_handles_thresholds_and_exceptions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")

    monkeypatch.setattr(pdf_extract, "extract_text_layer", lambda _pdf_path: "X" * 200)
    assert pdf_extract.detect_text_layer(pdf_path, 1) == (True, "X" * 200)

    monkeypatch.setattr(pdf_extract, "extract_text_layer", lambda _pdf_path: "short")
    assert pdf_extract.detect_text_layer(pdf_path, 2) == (False, "short")

    def boom(_pdf_path: Path) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(pdf_extract, "extract_text_layer", boom)
    assert pdf_extract.detect_text_layer(pdf_path, 1) == (False, "")


def test_extract_via_ocr_streams_pages_and_warns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    calls: list[dict[str, object]] = []

    def fake_convert_from_path(path: str, **kwargs: object) -> list[_FakeImage]:
        calls.append({"path": path, **kwargs})
        if kwargs.get("first_page") is None:
            return [_FakeImage("full-1"), _FakeImage("full-2")]
        if kwargs.get("first_page") == 1:
            return [_FakeImage("page-1")]
        return []

    fake_pytesseract = SimpleNamespace(
        image_to_string=lambda img, lang, timeout: "" if img.label == "full-2" else f"{img.label}:{lang}:{timeout}"
    )
    fake_pdf2image = SimpleNamespace(convert_from_path=fake_convert_from_path)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_pytesseract)
    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)

    text, warnings = pdf_extract.extract_via_ocr(pdf_path, "eng", page_count=0)
    assert text == "full-1:eng:120\n\f\n"
    assert warnings == ["page 2: OCR returned no text"]

    text, warnings = pdf_extract.extract_via_ocr(pdf_path, "eng", page_count=2)
    assert text == "page-1:eng:120"
    assert warnings == ["OCR rendered 1/2 pages"]
    assert calls[0]["dpi"] == pdf_extract.OCR_DPI
    assert calls[0]["timeout"] == pdf_extract.OCR_RENDER_TIMEOUT_SECONDS


def test_extract_skips_existing_sidecars_and_prints_dry_run_summary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n")
    pdf_path.with_suffix(".txt").write_text("existing text", encoding="utf-8")
    pdf_path.with_suffix(".meta.json").write_text("{}", encoding="utf-8")

    assert pdf_extract.extract(pdf_path, force=False, lang=None, dry_run=False) == 0
    out = capsys.readouterr().out
    assert "skip: sample.pdf" in out

    pdf_path.with_suffix(".txt").unlink()
    pdf_path.with_suffix(".meta.json").unlink()
    monkeypatch.setattr(pdf_extract, "get_page_count", lambda _pdf_path, warnings=None: 3)
    monkeypatch.setattr(pdf_extract, "detect_text_layer", lambda _pdf_path, _page_count: (True, ""))

    assert pdf_extract.extract(pdf_path, force=False, lang=None, dry_run=True) == 0
    out = capsys.readouterr().out
    assert "dry-run:" in out
    assert "create: sidecars will be generated" in out
