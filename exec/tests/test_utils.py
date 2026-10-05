import json
import sys

import pytest

from digtk.lib import derivative, utils


@pytest.fixture(autouse=True)
def reset_output_mode():
    """utils._output_mode is process-wide state set by main.py's --output option —
    reset it around every test so one test's mode never leaks into another."""
    utils.set_output_mode(None)
    yield
    utils.set_output_mode(None)


# --- output mode / result() / error() ----------------------------------------

def test_result_prints_json_when_mode_is_json(capsys):
    utils.set_output_mode("json")
    with pytest.raises(SystemExit) as exc_info:
        utils.result({"status": "success"})
    assert exc_info.value.code == 0
    assert json.loads(capsys.readouterr().out) == {"status": "success"}


def test_result_prints_nothing_when_mode_is_text(capsys):
    utils.set_output_mode("text")
    with pytest.raises(SystemExit):
        utils.result({"status": "success"})
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_result_auto_mode_prints_json_when_stdout_is_not_a_tty(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with pytest.raises(SystemExit):
        utils.result({"status": "success"})
    assert json.loads(capsys.readouterr().out) == {"status": "success"}


def test_result_auto_mode_prints_nothing_when_stdout_is_a_tty(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit):
        utils.result({"status": "success"})
    assert capsys.readouterr().out == ""


def test_error_prints_json_when_mode_is_json(capsys):
    utils.set_output_mode("json")
    with pytest.raises(SystemExit) as exc_info:
        utils.error("boom")
    assert exc_info.value.code == 1
    assert json.loads(capsys.readouterr().out) == {"status": "error", "error": "boom"}


def test_error_prints_plain_message_to_stderr_when_mode_is_text(capsys):
    utils.set_output_mode("text")
    with pytest.raises(SystemExit) as exc_info:
        utils.error("boom")
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Error: boom\n"


def test_error_respects_custom_exit_code():
    utils.set_output_mode("text")
    with pytest.raises(SystemExit) as exc_info:
        utils.error("boom", code=3)
    assert exc_info.value.code == 3


def test_write_manifest_writes_expected_shape(monkeypatch):
    calls = []
    monkeypatch.setattr(derivative, "write", lambda **kwargs: calls.append(kwargs))

    manifest = utils.write_manifest(
        "my-coll",
        "box-1/folder-3/derivatives",
        command=["digtk", "image", "derivatives", "ark:/87287/d7abc1234"],
        params={"widths": [256, 512, 1024]},
        outputs=["thumb-256.jpg", "thumb-512.jpg", "web-1024.jpg"],
        source_files=["cask:/bronze/g/my-coll/latest/box-1/folder-3/photo.tiff"],
        opts={"group": "g", "duration_s": 1.4},
    )

    assert manifest["status"] == "success"
    assert manifest["command"] == ["digtk", "image", "derivatives", "ark:/87287/d7abc1234"]
    assert manifest["params"] == {"widths": [256, 512, 1024]}
    assert manifest["outputs"] == ["thumb-256.jpg", "thumb-512.jpg", "web-1024.jpg"]
    assert manifest["duration_s"] == 1.4
    assert manifest["written_at"].endswith("Z")

    written = calls[0]
    assert written["type"] == "silver"
    assert written["collection"] == "my-coll"
    assert written["rel_path"] == "box-1/folder-3/derivatives/manifest.json"
    assert written["source_files"] == ["cask:/bronze/g/my-coll/latest/box-1/folder-3/photo.tiff"]
    assert written["opts"] == {"group": "g", "time": None, "replace": True}
    assert json.loads(written["data"]) == manifest


def test_write_manifest_strips_trailing_slash_from_rel_path(monkeypatch):
    calls = []
    monkeypatch.setattr(derivative, "write", lambda **kwargs: calls.append(kwargs))
    utils.write_manifest("c", "box-1/ocr/", [], {}, [], source_files=["cask:/bronze/g/c/latest/box-1/x.tiff"], opts={"group": "g"})
    assert calls[0]["rel_path"] == "box-1/ocr/manifest.json"


def test_write_manifest_defaults_status_to_success(monkeypatch):
    monkeypatch.setattr(derivative, "write", lambda **kwargs: None)
    manifest = utils.write_manifest("c", "x", [], {}, [], source_files=["cask:/bronze/g/c/latest/x/y.tiff"], opts={"group": "g"})
    assert manifest["status"] == "success"
    assert "error" not in manifest


def test_write_manifest_records_error_status(monkeypatch):
    monkeypatch.setattr(derivative, "write", lambda **kwargs: None)
    manifest = utils.write_manifest(
        "c", "x", [], {}, [], source_files=["cask:/bronze/g/c/latest/x/y.tiff"],
        opts={"group": "g", "status": "error", "error": "convert failed"},
    )
    assert manifest["status"] == "error"
    assert manifest["error"] == "convert failed"


def test_write_manifest_passes_time_through_to_derivative_write(monkeypatch):
    calls = []
    monkeypatch.setattr(derivative, "write", lambda **kwargs: calls.append(kwargs))
    utils.write_manifest("c", "x", [], {}, [], source_files=["cask:/bronze/g/c/2026-01-01/x/y.tiff"], opts={"group": "g", "time": "2026-01-01"})
    assert calls[0]["opts"]["time"] == "2026-01-01"


def test_write_manifest_merges_extra_top_level_keys(monkeypatch):
    monkeypatch.setattr(derivative, "write", lambda **kwargs: None)
    manifest = utils.write_manifest(
        "c", "x", [], {}, ["small.jpg"], source_files=["cask:/bronze/g/c/latest/x/y.tiff"],
        opts={"group": "g", "extra": {"small": {"size": {"width": 256, "height": 326}, "url": "cask://silver/g/c/latest/x/small.jpg"}}},
    )
    assert manifest["small"] == {"size": {"width": 256, "height": 326}, "url": "cask://silver/g/c/latest/x/small.jpg"}
    assert manifest["outputs"] == ["small.jpg"]


def test_write_manifest_without_extra_has_no_extra_keys(monkeypatch):
    monkeypatch.setattr(derivative, "write", lambda **kwargs: None)
    manifest = utils.write_manifest("c", "x", [], {}, [], source_files=["cask:/bronze/g/c/latest/x/y.tiff"], opts={"group": "g"})
    assert set(manifest.keys()) == {"written_at", "status", "command", "params", "outputs", "duration_s"}


def test_warn_prints_to_stderr_and_does_not_exit(capsys):
    utils.warn("page 3 skipped")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "Warning: page 3 skipped\n"
