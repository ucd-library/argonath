import json

import pytest
from typer.testing import CliRunner

from digtk.commands import image
from digtk.models.digital_website import image as image_model

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_thumbnail_calls_image_with_parsed_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(image_model, "thumbnails", lambda ark, opts={}: seen.update(ark=ark, opts=opts) or {"status": "success"})

    result = runner.invoke(image.app, ["thumbnail", ARK, "--widths", "256,512", "--quality", "80"])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert seen["opts"] == {"widths": "256,512", "quality": 80, "app": "digital-dev"}
    assert last_json_line(result.output) == {"status": "success"}


def test_thumbnail_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise LookupError("no such item")
    monkeypatch.setattr(image_model, "thumbnails", raises)

    result = runner.invoke(image.app, ["thumbnail", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no such item"}


def test_convert_calls_image_with_format(monkeypatch):
    seen = {}
    monkeypatch.setattr(image_model, "convert", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(image.app, ["convert", ARK, "--format", "png"])

    assert result.exit_code == 0
    assert seen["opts"] == {"format": "png", "app": "digital-dev"}


def test_convert_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise ValueError("bad format")
    monkeypatch.setattr(image_model, "convert", raises)

    result = runner.invoke(image.app, ["convert", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "bad format"}


def test_deskew_calls_image_with_threshold(monkeypatch):
    seen = {}
    monkeypatch.setattr(image_model, "deskew", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(image.app, ["deskew", ARK, "--deskew-threshold", "25"])

    assert result.exit_code == 0
    assert seen["opts"]["threshold"] == 25


def test_deskew_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise OSError("disk full")
    monkeypatch.setattr(image_model, "deskew", raises)

    result = runner.invoke(image.app, ["deskew", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "disk full"}


def test_prep_ocr_calls_image_with_deskew_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(image_model, "prep_ocr", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(image.app, ["prep-ocr", ARK, "--deskew"])

    assert result.exit_code == 0
    assert seen["opts"]["deskew"] is True


def test_prep_ocr_defaults_deskew_to_false(monkeypatch):
    seen = {}
    monkeypatch.setattr(image_model, "prep_ocr", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    runner.invoke(image.app, ["prep-ocr", ARK])

    assert seen["opts"]["deskew"] is False


def test_prep_ocr_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise LookupError("no such item")
    monkeypatch.setattr(image_model, "prep_ocr", raises)

    result = runner.invoke(image.app, ["prep-ocr", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no such item"}


def test_derivatives_calls_image_generate(monkeypatch):
    seen = {}

    def fake_generate(ark, opts={}):
        seen.update(ark=ark, opts=opts)
        return {"status": "success", "orientation": {"photo": {"deskew": True, "stats": {}}}}

    monkeypatch.setattr(image_model, "generate", fake_generate)

    result = runner.invoke(image.app, ["derivatives", ARK, "--widths", "256,1024"])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert seen["opts"]["widths"] == "256,1024"
    assert last_json_line(result.output) == {"status": "success", "orientation": {"photo": {"deskew": True, "stats": {}}}}
    assert "photo: deskewed" in result.output


def test_derivatives_logs_orientation_for_every_image(monkeypatch):
    def fake_generate(ark, opts={}):
        return {
            "status": "success",
            "orientation": {
                "photo-a": {"deskew": True, "stats": {}},
                "photo-b": {"deskew": False, "stats": {}},
            },
        }

    monkeypatch.setattr(image_model, "generate", fake_generate)

    result = runner.invoke(image.app, ["derivatives", ARK])

    assert result.exit_code == 0
    assert "photo-a: deskewed" in result.output
    assert "photo-b: original" in result.output


def test_derivatives_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise RuntimeError("convert exploded")
    monkeypatch.setattr(image_model, "generate", raises)

    result = runner.invoke(image.app, ["derivatives", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "convert exploded"}
