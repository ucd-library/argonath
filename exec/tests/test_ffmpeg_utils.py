import json
import shutil
import subprocess

import pytest

from digtk.lib import ffmpeg_utils

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


class FakeCompletedProcess:
    def __init__(self, stdout=""):
        self.stdout = stdout


@pytest.fixture
def fake_run(monkeypatch):
    calls = []

    def _install(stdout="", raises=None):
        def fake(argv, **kwargs):
            calls.append({"argv": argv, "kwargs": kwargs})
            if raises is not None:
                raise raises
            return FakeCompletedProcess(stdout=stdout)
        monkeypatch.setattr(subprocess, "run", fake)
        return calls

    return _install


# --- probe / has_audio_stream ---------------------------------------------------

def test_probe_parses_duration_bitrate_and_streams(fake_run):
    stdout = json.dumps({
        "format": {"duration": "12.345000", "bit_rate": "128000"},
        "streams": [
            {"codec_name": "h264", "codec_type": "video", "width": 640, "height": 480},
            {"codec_name": "aac", "codec_type": "audio"},
        ],
    })
    fake_run(stdout=stdout)
    result = ffmpeg_utils.probe("/tmp/video.mp4")
    assert result["duration_s"] == pytest.approx(12.345)
    assert result["bit_rate"] == 128000
    assert result["streams"] == [
        {"codec_name": "h264", "codec_type": "video", "width": 640, "height": 480},
        {"codec_name": "aac", "codec_type": "audio", "width": None, "height": None},
    ]


def test_probe_handles_missing_format_fields(fake_run):
    fake_run(stdout=json.dumps({"format": {}, "streams": []}))
    result = ffmpeg_utils.probe("/tmp/video.mp4")
    assert result == {"duration_s": None, "bit_rate": None, "streams": []}


def test_has_audio_stream_true_for_nonempty_output(fake_run):
    fake_run(stdout="[STREAM]\ncodec_name=aac\n[/STREAM]\n")
    assert ffmpeg_utils.has_audio_stream("/tmp/video.mp4") is True


def test_has_audio_stream_false_for_empty_output(fake_run):
    fake_run(stdout="")
    assert ffmpeg_utils.has_audio_stream("/tmp/silent.mp4") is False


# --- _ensure_audio_track ---------------------------------------------------------

def test_ensure_audio_track_passes_through_when_audio_present(fake_run, monkeypatch):
    monkeypatch.setattr(ffmpeg_utils, "has_audio_stream", lambda path, opts={}: True)
    calls = fake_run()
    result = ffmpeg_utils._ensure_audio_track("/tmp/video.mp4", "/tmp", {})
    assert result == "/tmp/video.mp4"
    assert calls == []


def test_ensure_audio_track_synthesizes_silent_track_when_missing(fake_run, monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_utils, "has_audio_stream", lambda path, opts={}: False)
    calls = fake_run()
    result = ffmpeg_utils._ensure_audio_track("/tmp/silent.mp4", str(tmp_path), {})
    assert result == str(tmp_path / "silent-with-audio.mp4")
    argv = calls[0]["argv"]
    assert "anullsrc" in argv
    assert "-shortest" in argv
    assert argv[argv.index("-c:a") + 1] == "aac"


# --- transcode_hls_rung / hls_ladder ----------------------------------------------

def test_transcode_hls_rung_builds_expected_argv(fake_run, tmp_path):
    calls = fake_run()
    result = ffmpeg_utils.transcode_hls_rung("/tmp/src.mp4", str(tmp_path), "480p")
    argv = calls[0]["argv"]
    assert argv[argv.index("-b:v") + 1] == "1400k"
    assert argv[argv.index("-b:a") + 1] == "128k"
    assert argv[argv.index("-crf") + 1] == "20"
    assert argv[-1] == str(tmp_path / "480p.m3u8")
    assert argv[argv.index("-hls_segment_filename") + 1] == str(tmp_path / "480p.mp4")
    assert result["playlist_path"] == str(tmp_path / "480p.m3u8")
    assert result["segment_path"] == str(tmp_path / "480p.mp4")


def test_transcode_hls_rung_rejects_unknown_resolution(fake_run, tmp_path):
    with pytest.raises(ValueError, match="unknown resolution"):
        ffmpeg_utils.transcode_hls_rung("/tmp/src.mp4", str(tmp_path), "4k")


def test_hls_ladder_writes_master_playlist_unconditionally(fake_run, monkeypatch, tmp_path):
    monkeypatch.setattr(ffmpeg_utils, "has_audio_stream", lambda path, opts={}: True)
    fake_run()
    result = ffmpeg_utils.hls_ladder("/tmp/src.mp4", str(tmp_path), ["360p", "720p"])

    assert len(result["rungs"]) == 2
    master = tmp_path / "playlist.m3u8"
    assert master.exists()
    content = master.read_text()
    assert "360p.m3u8" in content
    assert "720p.m3u8" in content
    assert "1080p.m3u8" not in content


def test_hls_ladder_ensures_audio_before_transcoding(fake_run, monkeypatch, tmp_path):
    ensure_calls = []

    def fake_ensure(src_path, tmp_dir, opts={}):
        ensure_calls.append(src_path)
        return "/tmp/synthesized-with-audio.mp4"

    monkeypatch.setattr(ffmpeg_utils, "_ensure_audio_track", fake_ensure)
    calls = fake_run()
    ffmpeg_utils.hls_ladder("/tmp/silent.mp4", str(tmp_path), ["360p"])

    assert ensure_calls == ["/tmp/silent.mp4"]
    assert calls[0]["argv"][calls[0]["argv"].index("-i") + 1] == "/tmp/synthesized-with-audio.mp4"


# --- extract_poster / transcode_audio / waveform ----------------------------------

def test_extract_poster_builds_expected_argv(fake_run, tmp_path):
    calls = fake_run()
    dest = str(tmp_path / "poster.jpg")
    result = ffmpeg_utils.extract_poster("/tmp/src.mp4", dest, opts={"timestamp_s": 2.5, "width": 512})
    argv = calls[0]["argv"]
    assert argv[argv.index("-ss") + 1] == "2.5"
    assert argv[argv.index("-vf") + 1] == "scale=512:-2"
    assert result["dest_path"] == dest


def test_transcode_audio_picks_codec_from_extension(fake_run, tmp_path):
    calls = fake_run()
    ffmpeg_utils.transcode_audio("/tmp/src.wav", str(tmp_path / "out.mp3"))
    argv = calls[0]["argv"]
    assert argv[argv.index("-c:a") + 1] == "libmp3lame"
    assert "-vn" in argv


def test_transcode_audio_honors_explicit_codec_override(fake_run, tmp_path):
    calls = fake_run()
    ffmpeg_utils.transcode_audio("/tmp/src.wav", str(tmp_path / "out.m4a"), opts={"codec": "alac"})
    argv = calls[0]["argv"]
    assert argv[argv.index("-c:a") + 1] == "alac"


def test_waveform_builds_expected_argv(fake_run, tmp_path):
    calls = fake_run()
    dest = str(tmp_path / "wave.png")
    ffmpeg_utils.waveform("/tmp/src.wav", dest, opts={"width": 200, "height": 80, "color": "0xff0000"})
    argv = calls[0]["argv"]
    assert argv[argv.index("-filter_complex") + 1] == "showwavespic=s=200x80:colors=0xff0000"


# --- error handling ---------------------------------------------------------------

def test_run_raises_ffmpeg_error_on_nonzero_exit(fake_run, tmp_path):
    fake_run(raises=subprocess.CalledProcessError(1, ["ffmpeg"], stderr="boom"))
    with pytest.raises(ffmpeg_utils.FFmpegError) as exc_info:
        ffmpeg_utils.transcode_hls_rung("/tmp/src.mp4", str(tmp_path), "360p")
    assert exc_info.value.returncode == 1
    assert exc_info.value.timed_out is False


def test_run_raises_ffmpeg_error_on_timeout(fake_run, tmp_path):
    fake_run(raises=subprocess.TimeoutExpired(cmd=["ffmpeg"], timeout=60))
    with pytest.raises(ffmpeg_utils.FFmpegError) as exc_info:
        ffmpeg_utils.transcode_hls_rung("/tmp/src.mp4", str(tmp_path), "360p")
    assert exc_info.value.timed_out is True


def test_run_never_uses_shell(fake_run, tmp_path):
    calls = fake_run()
    ffmpeg_utils.extract_poster("/tmp/src.mp4", str(tmp_path / "poster.jpg"))
    assert calls[0]["kwargs"].get("shell", False) is False


# --- end-to-end against real ffmpeg -----------------------------------------------

@needs_ffmpeg
def test_probe_reads_real_video(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest", str(src)],
        capture_output=True, check=True,
    )
    result = ffmpeg_utils.probe(str(src))
    assert result["duration_s"] == pytest.approx(1.0, abs=0.1)
    codec_types = {s["codec_type"] for s in result["streams"]}
    assert codec_types == {"video", "audio"}


@needs_ffmpeg
def test_hls_ladder_produces_playable_output(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest", str(src)],
        capture_output=True, check=True,
    )
    dest_dir = tmp_path / "stream"
    dest_dir.mkdir()
    result = ffmpeg_utils.hls_ladder(str(src), str(dest_dir), ["360p"])
    assert (dest_dir / "360p.m3u8").exists()
    assert (dest_dir / "360p.mp4").exists()
    assert (dest_dir / "playlist.m3u8").exists()


@needs_ffmpeg
def test_extract_poster_writes_real_jpeg(tmp_path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=5", str(src)],
        capture_output=True, check=True,
    )
    dest = tmp_path / "poster.jpg"
    ffmpeg_utils.extract_poster(str(src), str(dest), opts={"timestamp_s": 0.1, "width": 32})
    assert dest.exists()
