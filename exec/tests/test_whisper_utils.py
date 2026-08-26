import shutil
import subprocess

import pytest

from digtk.lib import whisper_utils

needs_whisper = pytest.mark.skipif(
    shutil.which("whisper") is None or shutil.which("ffmpeg") is None,
    reason="whisper/ffmpeg not installed",
)


@pytest.fixture
def fake_run(monkeypatch):
    """Replaces subprocess.run with a recorder that also touches whisper's expected
    <src-stem>.vtt output (unless write_output=False, to simulate a silent success
    that never actually wrote anything)."""
    calls = []

    def _install(raises=None, write_output=True, content="WEBVTT\n"):
        def fake(argv, **kwargs):
            calls.append({"argv": argv, "kwargs": kwargs})
            if raises is not None:
                raise raises
            if write_output:
                src_path = argv[1]
                output_dir = argv[argv.index("--output_dir") + 1]
                stem = src_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
                with open(f"{output_dir}/{stem}.vtt", "w") as f:
                    f.write(content)
            return subprocess.CompletedProcess(argv, 0)
        monkeypatch.setattr(subprocess, "run", fake)
        return calls

    return _install


def test_transcribe_builds_expected_argv(fake_run, tmp_path):
    calls = fake_run()
    result = whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(tmp_path / "out.vtt"))
    argv = calls[0]["argv"]
    assert argv[0] == "whisper"
    assert argv[1] == str(tmp_path / "src.mp4")
    assert argv[argv.index("--language") + 1] == "en"
    assert argv[argv.index("--model") + 1] == "small"
    assert argv[argv.index("--output_format") + 1] == "vtt"
    assert result["dest_path"] == str(tmp_path / "out.vtt")


def test_transcribe_renames_whisper_output_to_dest_path(fake_run, tmp_path):
    fake_run(content="WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n")
    dest = tmp_path / "captions" / "output-en.vtt"
    whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(dest))
    assert dest.exists()
    assert dest.read_text() == "WEBVTT\n\n00:00.000 --> 00:01.000\nhello\n"
    # whisper's own default-named output must not be left behind
    assert not (tmp_path / "captions" / "src.vtt").exists()


def test_transcribe_honors_language_and_model_opts(fake_run, tmp_path):
    calls = fake_run()
    whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(tmp_path / "out.vtt"), opts={"language": "es", "model": "medium"})
    argv = calls[0]["argv"]
    assert argv[argv.index("--language") + 1] == "es"
    assert argv[argv.index("--model") + 1] == "medium"


def test_transcribe_rejects_unknown_model(fake_run, tmp_path):
    fake_run()
    with pytest.raises(ValueError, match="unknown whisper model"):
        whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(tmp_path / "out.vtt"), opts={"model": "huge"})


def test_transcribe_creates_parent_dirs(fake_run, tmp_path):
    fake_run()
    dest = tmp_path / "sub" / "deeper" / "out.vtt"
    whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(dest))
    assert dest.parent.is_dir()


def test_transcribe_raises_whisper_error_on_failure(fake_run, tmp_path):
    fake_run(raises=subprocess.CalledProcessError(1, ["whisper"], stderr="model download failed"))
    with pytest.raises(whisper_utils.WhisperError) as exc_info:
        whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(tmp_path / "out.vtt"))
    assert exc_info.value.returncode == 1
    assert exc_info.value.timed_out is False


def test_transcribe_raises_whisper_error_on_timeout(fake_run, tmp_path):
    fake_run(raises=subprocess.TimeoutExpired(cmd=["whisper"], timeout=3600))
    with pytest.raises(whisper_utils.WhisperError) as exc_info:
        whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(tmp_path / "out.vtt"))
    assert exc_info.value.timed_out is True


def test_transcribe_raises_whisper_error_on_missing_output(fake_run, tmp_path):
    fake_run(write_output=False)
    with pytest.raises(whisper_utils.WhisperError, match="did not write"):
        whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(tmp_path / "out.vtt"))


def test_run_never_uses_shell(fake_run, tmp_path):
    calls = fake_run()
    whisper_utils.transcribe(str(tmp_path / "src.mp4"), str(tmp_path / "out.vtt"))
    assert calls[0]["kwargs"].get("shell", False) is False


def test_is_available_false_when_binary_missing(monkeypatch):
    monkeypatch.setattr(whisper_utils.shutil, "which", lambda name: None)
    assert whisper_utils.is_available() is False


def test_is_available_true_when_help_succeeds(monkeypatch):
    monkeypatch.setattr(whisper_utils.shutil, "which", lambda name: "/usr/local/bin/whisper")
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 0))
    assert whisper_utils.is_available() is True


# --- end-to-end against real whisper -----------------------------------------

@needs_whisper
def test_transcribe_real_tiny_model(tmp_path):
    src = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-ar", "16000", str(src)],
        capture_output=True, check=True,
    )
    dest = tmp_path / "output-en.vtt"
    result = whisper_utils.transcribe(str(src), str(dest), opts={"model": "tiny", "timeout": 120})
    assert dest.exists()
    assert dest.read_text().startswith("WEBVTT")
    assert result["dest_path"] == str(dest)
