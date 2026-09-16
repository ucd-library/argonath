import json

from typer.testing import CliRunner

from digtk.commands import audio
from digtk.models.digital_website import audio as audio_model

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_probe_calls_audio(monkeypatch):
    seen = {}
    monkeypatch.setattr(audio_model, "probe", lambda ark, opts={}: seen.update(ark=ark) or {"status": "success", "streams": {}})

    result = runner.invoke(audio.app, ["probe", ARK])

    assert result.exit_code == 0
    assert seen["ark"] == ARK


def test_transcode_calls_audio_with_format_and_bitrate(monkeypatch):
    seen = {}
    monkeypatch.setattr(audio_model, "transcode", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(audio.app, ["transcode", ARK, "--format", "aac", "--bitrate", "128k"])

    assert result.exit_code == 0
    assert seen["opts"] == {"format": "aac", "bitrate": "128k", "app": "digital-dev"}


def test_transcode_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise ValueError("bad format")
    monkeypatch.setattr(audio_model, "transcode", raises)

    result = runner.invoke(audio.app, ["transcode", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "bad format"}


def test_waveform_calls_audio_with_dimensions(monkeypatch):
    seen = {}
    monkeypatch.setattr(audio_model, "waveform", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    result = runner.invoke(audio.app, ["waveform", ARK, "--width", "200", "--height", "80"])

    assert result.exit_code == 0
    assert seen["opts"] == {"width": 200, "height": 80, "app": "digital-dev"}


def test_derivatives_calls_audio(monkeypatch):
    seen = {}
    monkeypatch.setattr(audio_model, "derivatives", lambda ark, opts={}: seen.update(ark=ark, opts=opts) or {"status": "success"})

    result = runner.invoke(audio.app, ["derivatives", ARK, "--format", "mp3"])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert seen["opts"]["format"] == "mp3"


def test_derivatives_forwards_caption_language_and_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(audio_model, "derivatives", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    runner.invoke(audio.app, ["derivatives", ARK, "--language", "es", "--model", "medium"])

    assert seen["opts"]["language"] == "es"
    assert seen["opts"]["model"] == "medium"


def test_derivatives_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise RuntimeError("ffmpeg exploded")
    monkeypatch.setattr(audio_model, "derivatives", raises)

    result = runner.invoke(audio.app, ["derivatives", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "ffmpeg exploded"}
