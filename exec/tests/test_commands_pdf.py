import json

from typer.testing import CliRunner

from digtk.commands import pdf
from digtk.models.digital_website import pdf as pdf_model

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_page_count_calls_pdf(monkeypatch):
    seen = {}
    monkeypatch.setattr(pdf_model, "page_count", lambda ark, opts={}: seen.update(ark=ark) or {"status": "success", "pages": {"document": 3}})

    result = runner.invoke(pdf.app, ["page-count", ARK])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert last_json_line(result.output) == {"status": "success", "pages": {"document": 3}}


def test_page_count_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise LookupError("no such item")
    monkeypatch.setattr(pdf_model, "page_count", raises)

    result = runner.invoke(pdf.app, ["page-count", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no such item"}


def test_thumbnail_calls_pdf_with_page_and_widths(monkeypatch):
    seen = {}
    monkeypatch.setattr(pdf_model, "thumbnail", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(pdf.app, ["thumbnail", ARK, "--page", "2", "--widths", "256,512"])

    assert result.exit_code == 0
    assert seen["opts"] == {"page": 2, "widths": "256,512", "quality": 90}


def test_thumbnail_defaults_page_to_zero(monkeypatch):
    seen = {}
    monkeypatch.setattr(pdf_model, "thumbnail", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    runner.invoke(pdf.app, ["thumbnail", ARK])

    assert seen["opts"]["page"] == 0


def test_convert_calls_pdf_with_page_and_format(monkeypatch):
    seen = {}
    monkeypatch.setattr(pdf_model, "convert", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(pdf.app, ["convert", ARK, "--page", "1", "--format", "png"])

    assert result.exit_code == 0
    assert seen["opts"] == {"page": 1, "format": "png"}


def test_deskew_calls_pdf_with_page_and_threshold(monkeypatch):
    seen = {}
    monkeypatch.setattr(pdf_model, "deskew", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(pdf.app, ["deskew", ARK, "--page", "3", "--deskew-threshold", "25"])

    assert result.exit_code == 0
    assert seen["opts"]["page"] == 3
    assert seen["opts"]["threshold"] == 25


def test_prep_ocr_calls_pdf_with_page_and_deskew_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(pdf_model, "prep_ocr", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(pdf.app, ["prep-ocr", ARK, "--page", "1", "--deskew"])

    assert result.exit_code == 0
    assert seen["opts"]["page"] == 1
    assert seen["opts"]["deskew"] is True


def test_prep_ocr_defaults_deskew_to_false(monkeypatch):
    seen = {}
    monkeypatch.setattr(pdf_model, "prep_ocr", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    runner.invoke(pdf.app, ["prep-ocr", ARK])

    assert seen["opts"]["deskew"] is False


def test_derivatives_calls_pdf(monkeypatch):
    seen = {}

    def fake_derivatives(ark, opts={}):
        seen.update(ark=ark, opts=opts)
        return {
            "status": "success",
            "pdfs": {"document": {"orientation": {"page-000": {"deskew": True, "stats": {}}}}},
        }

    monkeypatch.setattr(pdf_model, "derivatives", fake_derivatives)

    result = runner.invoke(pdf.app, ["derivatives", ARK, "--widths", "256,1024"])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert seen["opts"]["widths"] == "256,1024"
    assert "document page-000: deskewed" in result.output


def test_derivatives_logs_orientation_for_every_page(monkeypatch):
    def fake_derivatives(ark, opts={}):
        return {
            "status": "success",
            "pdfs": {
                "document": {
                    "orientation": {
                        "page-000": {"deskew": True, "stats": {}},
                        "page-001": {"deskew": False, "stats": {}},
                    },
                },
            },
        }

    monkeypatch.setattr(pdf_model, "derivatives", fake_derivatives)

    result = runner.invoke(pdf.app, ["derivatives", ARK])

    assert result.exit_code == 0
    assert "document page-000: deskewed" in result.output
    assert "document page-001: original" in result.output


def test_derivatives_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise RuntimeError("rasterize exploded")
    monkeypatch.setattr(pdf_model, "derivatives", raises)

    result = runner.invoke(pdf.app, ["derivatives", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "rasterize exploded"}


def test_derivatives_file_option_is_forwarded(monkeypatch):
    seen = {}
    monkeypatch.setattr(pdf_model, "derivatives", lambda ark, opts={}: seen.update(opts=opts) or {
        "status": "success", "pdfs": {},
    })

    result = runner.invoke(pdf.app, ["derivatives", ARK, "--file", "cask:/bronze/g/c/latest/item/doc.pdf"])

    assert result.exit_code == 0
    assert seen["opts"]["files"] == ["cask:/bronze/g/c/latest/item/doc.pdf"]
