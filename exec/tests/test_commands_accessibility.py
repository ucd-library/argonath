import json

import pytest
from typer.testing import CliRunner

from digtk.lib import items
from digtk.lib.items import ItemLocation
from digtk.models import workspace as workspace_model
from digtk.main import app
from digtk.models import accessibility as accessibility_model

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


def test_alt_text_calls_model_with_default_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(accessibility_model, "generate_alt_text",
                         lambda ark, opts={}: seen.update(ark=ark, opts=opts) or {"status": "success"})

    result = runner.invoke(app, ["accessibility", "alt-text", ARK])

    assert result.exit_code == 0
    assert seen["ark"].ark == ARK
    assert seen["opts"] == {
        "collection_context": "", "name_people": True, "transcribe_text": True,
        "want_description": False, "max_alt_chars": 150,
    }
    assert last_json_line(result.output) == {"status": "success"}


def test_alt_text_forwards_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(accessibility_model, "generate_alt_text",
                         lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(app, [
        "accessibility", "alt-text", ARK,
        "--collection-context", "Wine collection papers",
        "--no-name-people", "--no-transcribe-text",
        "--want-description", "--max-alt-chars", "100",
    ])

    assert result.exit_code == 0
    assert seen["opts"] == {
        "collection_context": "Wine collection papers", "name_people": False,
        "transcribe_text": False, "want_description": True, "max_alt_chars": 100,
    }


def test_alt_text_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise LookupError("no such item")
    monkeypatch.setattr(accessibility_model, "generate_alt_text", raises)

    result = runner.invoke(app, ["accessibility", "alt-text", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no such item"}


def test_tag_pdf_calls_model_with_default_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(accessibility_model, "tag_pdf",
                         lambda ark, opts={}: seen.update(ark=ark, opts=opts) or {"status": "success"})

    result = runner.invoke(app, ["accessibility", "tag-pdf", ARK])

    assert result.exit_code == 0
    assert seen["ark"].ark == ARK
    assert seen["opts"] == {
        "lang": "en-US", "strict": False, "lookahead": 40, "tables": False, "validate": True,
        "min_conf": 0.0,
    }
    assert last_json_line(result.output) == {"status": "success"}


def test_tag_pdf_forwards_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(accessibility_model, "tag_pdf",
                         lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(app, [
        "accessibility", "tag-pdf", ARK,
        "--lang", "en", "--strict", "--lookahead", "80", "--tables", "--no-validate",
        "--min-conf", "65",
    ])

    assert result.exit_code == 0
    assert seen["opts"] == {
        "lang": "en", "strict": True, "lookahead": 80, "tables": True, "validate": False,
        "min_conf": 65.0,
    }


def test_tag_pdf_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise FileNotFoundError("OCR-prep image not found")
    monkeypatch.setattr(accessibility_model, "tag_pdf", raises)

    result = runner.invoke(app, ["accessibility", "tag-pdf", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "OCR-prep image not found"}


def test_alt_text_and_tag_pdf_report_under_their_own_stages(monkeypatch):
    seen = []
    monkeypatch.setattr(workspace_model, "apply_to_targets",
                         lambda target, fn, opts={}: seen.append((opts["stage"], opts["reporting_job_id"])) or {"status": "success"})

    runner.invoke(app, ["accessibility", "alt-text", ARK, "--reporting-job-id", "run-1"])
    runner.invoke(app, ["accessibility", "tag-pdf", ARK, "--reporting-job-id", "run-1"])

    assert seen == [("alt-text", "run-1"), ("tag-pdf", "run-1")]
