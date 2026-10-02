import json

from typer.testing import CliRunner

from digtk.lib import items
from digtk.main import app
from digtk.models import workspace as workspace_model

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_run_splits_steps_and_builds_step_opts(monkeypatch):
    seen = {}
    monkeypatch.setattr(workspace_model, "run",
                         lambda target, opts={}: seen.update(target=target, opts=opts) or {"status": "success"})

    result = runner.invoke(app, [
        "workspace", "run", "./scans", "--steps", "ocr, tag-pdf",
        "--no-vision", "--tables", "--min-conf", "60", "--collection", "box-12",
        "--reporting-job-id", "run-1",
    ])

    assert result.exit_code == 0, result.output
    assert seen["target"] == "./scans"
    assert seen["opts"]["steps"] == ["ocr", "tag-pdf"]
    assert seen["opts"]["step_opts"]["ocr"]["vision"] is False
    assert seen["opts"]["step_opts"]["tag-pdf"] == {"lang": "en-US", "tables": True, "min_conf": 60.0, "validate": True}
    assert seen["opts"]["collection"] == "box-12"
    assert seen["opts"]["reporting_job_id"] == "run-1"
    assert seen["opts"]["files"] == []


def test_run_file_option_is_repeatable(monkeypatch):
    seen = {}
    monkeypatch.setattr(workspace_model, "run",
                         lambda target, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(app, ["workspace", "run", "cask:/workspace/jdoe/box-12/source", "--steps", "ocr",
                                 "--file", "a.tif", "--file", "b.pdf"])

    assert result.exit_code == 0, result.output
    assert seen["opts"]["files"] == ["a.tif", "b.pdf"]


def test_promote_forwards_ark_and_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(workspace_model, "promote", lambda path, ark, opts={}: seen.update(
        path=path, ark=ark, opts=opts,
    ) or {"status": "success", "promoted": []})

    result = runner.invoke(app, [
        "workspace", "promote", "cask:/workspace/jdoe/box-12/source", "--ark", ARK, "--dry-run",
    ])

    assert result.exit_code == 0, result.output
    assert seen == {"path": "cask:/workspace/jdoe/box-12/source", "ark": ARK,
                    "opts": {"replace": False, "dry_run": True}}


def test_ls_returns_collections(monkeypatch):
    collections = [{"username": "jdoe", "collection": "box-12", "key": "jdoe/box-12", "path": "cask:/workspace/jdoe/box-12"}]
    monkeypatch.setattr(workspace_model, "list_collections", lambda opts={}: collections)

    result = runner.invoke(app, ["workspace", "ls"])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"status": "success", "collections": collections}


def test_run_routes_errors_through_utils_error(monkeypatch):
    def raises(target, opts={}):
        raise ValueError("steps must be a non-empty subset")
    monkeypatch.setattr(workspace_model, "run", raises)

    result = runner.invoke(app, ["workspace", "run", "./scans", "--steps", "nope"])

    assert result.exit_code == 1
    assert last_json_line(result.output)["status"] == "error"


def test_items_lists_workspace_item_dirs(monkeypatch):
    monkeypatch.setattr(items, "list_workspace_items", lambda path: [f"{path}/source"])

    result = runner.invoke(app, ["workspace", "items", "cask:/workspace/jdoe/box-12"])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"status": "success", "items": ["cask:/workspace/jdoe/box-12/source"]}


def test_items_with_files_lists_each_items_files(monkeypatch):
    listing = [{"item": "cask:/workspace/jdoe/box-12/source", "files": ["a.tif", "b.pdf"]}]
    monkeypatch.setattr(workspace_model, "list_items", lambda path: listing)

    result = runner.invoke(app, ["workspace", "items", "cask:/workspace/jdoe/box-12", "--files"])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"status": "success", "items": listing}
