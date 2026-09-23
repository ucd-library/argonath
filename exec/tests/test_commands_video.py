import json

from typer.testing import CliRunner

from digtk.commands import video
from digtk.models.digital_website import video as video_model

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_probe_calls_video(monkeypatch):
    seen = {}
    monkeypatch.setattr(video_model, "probe", lambda ark, opts={}: seen.update(ark=ark) or {"status": "success", "streams": {}})

    result = runner.invoke(video.app, ["probe", ARK])

    assert result.exit_code == 0
    assert seen["ark"] == ARK


def test_probe_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise LookupError("no such item")
    monkeypatch.setattr(video_model, "probe", raises)

    result = runner.invoke(video.app, ["probe", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no such item"}


def test_hls_calls_video_with_resolutions(monkeypatch):
    seen = {}
    monkeypatch.setattr(video_model, "hls", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(video.app, ["hls", ARK, "--resolutions", "360p,720p"])

    assert result.exit_code == 0
    assert seen["opts"] == {"resolutions": "360p,720p", "app": "digital-dev"}


def test_hls_defaults_to_configured_resolutions(monkeypatch):
    seen = {}
    monkeypatch.setattr(video_model, "hls", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    runner.invoke(video.app, ["hls", ARK])

    assert seen["opts"]["resolutions"] == "360p,480p,720p,1080p"


def test_derivatives_calls_video(monkeypatch):
    seen = {}
    monkeypatch.setattr(video_model, "derivatives", lambda ark, opts={}: seen.update(ark=ark, opts=opts) or {"status": "success"})

    result = runner.invoke(video.app, ["derivatives", ARK, "--resolutions", "360p"])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert seen["opts"]["resolutions"] == "360p"


def test_derivatives_forwards_caption_language_and_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(video_model, "derivatives", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    runner.invoke(video.app, ["derivatives", ARK, "--language", "es", "--model", "medium"])

    assert seen["opts"]["language"] == "es"
    assert seen["opts"]["model"] == "medium"


def test_derivatives_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(video_model, "derivatives", raises)

    result = runner.invoke(video.app, ["derivatives", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "ffmpeg exploded"}
