import json

import typer
from typer.testing import CliRunner

from digtk.commands import captions as captions_cmd
from digtk.models.digital_website import captions as captions_model

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"

# captions.app has exactly one command (transcribe) — Typer collapses a single-command
# app to a no-subcommand-name invocation when run standalone, which doesn't match how
# it's actually used (mounted under the main `digtk` app via add_typer, same as every
# other command group, where the subcommand name is always required). Mount it under a
# throwaway parent here too, so these tests exercise the real invocation shape.
parent = typer.Typer()
parent.add_typer(captions_cmd.app, name="captions")


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_transcribe_calls_captions_model_with_language_and_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(captions_model, "transcribe", lambda ark, opts={}: seen.update(ark=ark, opts=opts) or {"status": "success"})

    result = runner.invoke(parent, ["captions", "transcribe", ARK, "--language", "es", "--model", "medium"])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert seen["opts"] == {"language": "es", "model": "medium"}


def test_transcribe_defaults_to_configured_language_and_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(captions_model, "transcribe", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    runner.invoke(parent, ["captions", "transcribe", ARK])

    assert seen["opts"] == {"language": "en", "model": "small"}


def test_transcribe_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise RuntimeError("whisper exploded")
    monkeypatch.setattr(captions_model, "transcribe", raises)

    result = runner.invoke(parent, ["captions", "transcribe", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "whisper exploded"}
