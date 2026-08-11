"""
Whisper (openai-whisper CLI) speech-to-text wrapper for captions.

ARK/CaskFS-agnostic: every function takes and returns local filesystem paths only.
Ports dams-video-transcribe's lib/whisper.js — same shellout, same rename step (the
`whisper` CLI has no output-filename flag, only --output_dir, so it always writes
<src-stem>.vtt there and the caller renames it to whatever dest_path was requested).
Works unmodified on audio-only or video input, since whisper pulls audio from a video
file itself via ffmpeg internally.
"""
import os
import shutil
import subprocess

from digtk.lib.config import CONFIG

MODEL_CHOICES = ("tiny", "base", "small", "medium", "large")


class WhisperError(RuntimeError):
    """Raised when a whisper subprocess invocation fails, times out, or exits 0
    without writing its output file.

    :param list[str] argv: The command that was run.
    :param int|None returncode: Process exit code, or None if it timed out.
    :param str stderr: Captured stderr output.
    :param bool timed_out: True if the failure was a timeout rather than a non-zero exit.
    """

    def __init__(self, argv, returncode, stderr, timed_out=False):
        reason = "timed out" if timed_out else f"exited with code {returncode}"
        super().__init__(f"whisper command {reason}: {' '.join(argv)}\n{stderr}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out


def is_available(opts={}):
    """Whether the `whisper` CLI is installed and runnable.

    :param dict opts: 'whisper_bin' (str, default CONFIG["captions"]["whisper_bin"]).
    :returns: True if the binary is on PATH and `whisper --help` runs successfully.
    :rtype: bool
    """
    whisper_bin = opts.get("whisper_bin", CONFIG["captions"]["whisper_bin"])
    if shutil.which(whisper_bin) is None:
        return False
    try:
        subprocess.run([whisper_bin, "--help"], capture_output=True, check=True)
    except (subprocess.CalledProcessError, OSError):
        return False
    return True


def transcribe(src_path, dest_path, opts={}):
    """Transcribe an audio or video file to a WebVTT caption file.

    `whisper <src_path> --language {language} --model {model} --output_format vtt
    --output_dir {scratch_dir}`.

    :param str src_path: Local source audio/video path.
    :param str dest_path: Local destination .vtt path.
    :param dict opts: 'language' (str, default CONFIG["captions"]["language"]), 'model'
        (str, default CONFIG["captions"]["model"], must be one of MODEL_CHOICES),
        'whisper_bin' (str, default CONFIG["captions"]["whisper_bin"]), 'timeout'
        (float, default CONFIG["captions"]["timeout_s"]).
    :returns: {"argv": [...], "dest_path": str}
    :rtype: dict
    :raises ValueError: If 'model' isn't one of MODEL_CHOICES.
    :raises WhisperError: If `whisper` fails, times out, or exits 0 without writing
        its expected output file.
    """
    language = opts.get("language", CONFIG["captions"]["language"])
    model = opts.get("model", CONFIG["captions"]["model"])
    if model not in MODEL_CHOICES:
        raise ValueError(f"unknown whisper model {model!r} (use one of {MODEL_CHOICES})")
    whisper_bin = opts.get("whisper_bin", CONFIG["captions"]["whisper_bin"])
    timeout = opts.get("timeout", CONFIG["captions"]["timeout_s"])

    scratch_dir = os.path.dirname(os.path.abspath(dest_path))
    os.makedirs(scratch_dir, exist_ok=True)
    argv = [
        whisper_bin, src_path,
        "--language", language, "--model", model,
        "--output_format", "vtt", "--output_dir", scratch_dir,
    ]
    try:
        subprocess.run(argv, shell=False, check=True, timeout=timeout, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise WhisperError(argv, e.returncode, e.stderr) from e
    except subprocess.TimeoutExpired as e:
        raise WhisperError(argv, None, "", timed_out=True) from e

    stem = os.path.splitext(os.path.basename(src_path))[0]
    whisper_output = os.path.join(scratch_dir, f"{stem}.vtt")
    if not os.path.exists(whisper_output):
        raise WhisperError(argv, 0, f"whisper exited 0 but did not write {whisper_output}")
    if os.path.abspath(whisper_output) != os.path.abspath(dest_path):
        os.replace(whisper_output, dest_path)

    return {"argv": argv, "dest_path": dest_path}
