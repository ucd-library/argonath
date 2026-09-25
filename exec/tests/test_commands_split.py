import json

from typer.testing import CliRunner

from digtk.commands import split
from digtk.models import split as split_model

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_detect_calls_model_with_default_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(split_model, "detect",
                         lambda ark, opts={}: seen.update(ark=ark, opts=opts) or {"status": "success"})

    result = runner.invoke(split.app, ["detect", ARK])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert seen["opts"] == {"max_px": 1400}
    assert last_json_line(result.output) == {"status": "success"}


def test_detect_forwards_max_px(monkeypatch):
    seen = {}
    monkeypatch.setattr(split_model, "detect",
                         lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(split.app, ["detect", ARK, "--max-px", "800"])

    assert result.exit_code == 0
    assert seen["opts"] == {"max_px": 800}


def test_detect_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise ValueError("no image binaries found")
    monkeypatch.setattr(split_model, "detect", raises)

    result = runner.invoke(split.app, ["detect", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no image binaries found"}


def test_apply_calls_model_with_ark(monkeypatch):
    seen = {}
    monkeypatch.setattr(split_model, "apply",
                         lambda ark, opts={}: seen.update(ark=ark) or {"status": "success"})

    result = runner.invoke(split.app, ["apply", ARK])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert last_json_line(result.output) == {"status": "success"}


def test_apply_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise FileNotFoundError("no gutters.json")
    monkeypatch.setattr(split_model, "apply", raises)

    result = runner.invoke(split.app, ["apply", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no gutters.json"}
