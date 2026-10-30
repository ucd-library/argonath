import json

import pytest
from typer.testing import CliRunner

from digtk.lib import items
from digtk.lib.items import ItemLocation
from digtk.models import workspace as workspace_model
from digtk.main import app
from digtk.models import ocr as ocr_model

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"


@pytest.fixture(autouse=True)
def stub_resolve(monkeypatch):
    """ARK targets resolve through digtk.models.workspace.resolve_targets() ->
    items.resolve(); stub it so no CaskFS lookup happens."""
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: ItemLocation(
        ark=ark, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
    ))


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_tesseract_calls_model_with_default_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(ocr_model, "run_ocr",
                         lambda ark, opts={}: seen.update(ark=ark, opts=opts) or {"status": "success"})

    result = runner.invoke(app, ["ocr", "tesseract", ARK])

    assert result.exit_code == 0
    assert seen["ark"].ark == ARK
    assert seen["opts"]["vision"] is True
    assert last_json_line(result.output) == {"status": "success"}


def test_tesseract_no_vision_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(ocr_model, "run_ocr",
                         lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(app, ["ocr", "tesseract", ARK, "--no-vision"])

    assert result.exit_code == 0
    assert seen["opts"]["vision"] is False


def test_tesseract_forwards_ocr_enhance(monkeypatch):
    seen = {}
    monkeypatch.setattr(ocr_model, "run_ocr",
                         lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(app, ["ocr", "tesseract", ARK, "--ocr-enhance", "gray"])

    assert result.exit_code == 0
    assert seen["opts"]["enhance"] == "gray"


def test_tesseract_rejects_unknown_ocr_enhance(monkeypatch):
    monkeypatch.setattr(ocr_model, "run_ocr", lambda ark, opts={}: {"status": "success"})

    result = runner.invoke(app, ["ocr", "tesseract", ARK, "--ocr-enhance", "bright"])

    assert result.exit_code == 2


def test_tesseract_forwards_lang_psm_oem(monkeypatch):
    seen = {}
    monkeypatch.setattr(ocr_model, "run_ocr",
                         lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(app, ["ocr", "tesseract", ARK, "--lang", "fra", "--psm", "6", "--oem", "1"])

    assert result.exit_code == 0
    assert seen["opts"]["lang"] == "fra"
    assert seen["opts"]["psm"] == 6
    assert seen["opts"]["oem"] == 1


def test_tesseract_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise FileNotFoundError("OCR-prep image not found")
    monkeypatch.setattr(ocr_model, "run_ocr", raises)

    result = runner.invoke(app, ["ocr", "tesseract", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "OCR-prep image not found"}


def test_ml_command_no_longer_exists():
    result = runner.invoke(app, ["ocr", "ml", ARK])
    assert result.exit_code != 0


def test_tesseract_forwards_reporting_and_workspace_opts(monkeypatch):
    seen = {}
    monkeypatch.setattr(workspace_model, "apply_to_targets",
                         lambda target, fn, opts={}: seen.update(target=target, opts=opts) or {"status": "success"})

    result = runner.invoke(app, [
        "ocr", "tesseract", "./scans", "--folder", "box-12", "--user", "jdoe",
        "--reporting-job-id", "run-1", "--reporting-collection", "g/c",
    ])

    assert result.exit_code == 0
    assert seen["target"] == "./scans"
    assert seen["opts"]["stage"] == "ocr"
    assert seen["opts"]["folder"] == "box-12"
    assert seen["opts"]["username"] == "jdoe"
    assert seen["opts"]["reporting_job_id"] == "run-1"
    assert seen["opts"]["reporting_collection"] == "g/c"


def test_correct_runs_model_under_its_own_stage(monkeypatch):
    seen = {}
    monkeypatch.setattr(workspace_model, "apply_to_targets",
                         lambda target, fn, opts={}: seen.update(fn=fn, opts=opts) or {"status": "success"})

    result = runner.invoke(app, ["ocr", "correct", ARK, "--reporting-job-id", "run-1"])

    assert result.exit_code == 0, result.output
    assert seen["fn"] is ocr_model.correct_ocr
    assert seen["opts"]["stage"] == "ocr-correct"
    assert seen["opts"]["reporting_job_id"] == "run-1"


def test_tesseract_ocr_text_pages_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(ocr_model, "run_ocr",
                         lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    default = runner.invoke(app, ["ocr", "tesseract", ARK])
    assert default.exit_code == 0 and seen["opts"]["ocr_text_pages"] is False
    forced = runner.invoke(app, ["ocr", "tesseract", ARK, "--ocr-text-pages"])
    assert forced.exit_code == 0 and seen["opts"]["ocr_text_pages"] is True
