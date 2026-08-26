"""
ffmpeg/ffprobe subprocess wrapper.

ARK/CaskFS-agnostic: every function takes and returns local filesystem paths only.
Mirrors digtk.lib.image_magick's shape (list-args subprocess, never shell=True,
check=True, typed error class) — see that module's docstring for the rationale.

hls_ladder()'s codec/bitrate ladder is ported from the reference dams/services/image-utils
pipeline's lib/ffmpeg/convert.sh, run as one sequential pass instead of that pipeline's
per-resolution Cloud Run Job fan-out (see hls_ladder()'s own docstring). transcode_audio()
and waveform() have no equivalent in the reference — there is no prior convention to
match for these, only reasonable new defaults (see digtk.lib.config's CONFIG["audio"]).
"""
import json
import os
import subprocess

from digtk.lib.config import CONFIG


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe subprocess invocation fails or times out.

    :param list[str] argv: The command that was run.
    :param int|None returncode: Process exit code, or None if it timed out.
    :param str stderr: Captured stderr output.
    :param bool timed_out: True if the failure was a timeout rather than a non-zero exit.
    """

    def __init__(self, argv, returncode, stderr, timed_out=False):
        reason = "timed out" if timed_out else f"exited with code {returncode}"
        super().__init__(f"ffmpeg command {reason}: {' '.join(argv)}\n{stderr}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out


def _run(argv, opts={}):
    """Run an ffmpeg/ffprobe subprocess call safely.

    :param list[str] argv: Full command. Never shell-interpreted.
    :param dict opts: 'timeout' (float, seconds) overrides CONFIG["video"]["timeout_s"].
    :returns: The completed process' stdout.
    :rtype: str
    :raises FFmpegError: On non-zero exit or timeout.
    """
    timeout = opts.get("timeout", CONFIG["video"]["timeout_s"])
    try:
        result = subprocess.run(
            argv, shell=False, check=True, timeout=timeout,
            capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise FFmpegError(argv, e.returncode, e.stderr) from e
    except subprocess.TimeoutExpired as e:
        raise FFmpegError(argv, None, "", timed_out=True) from e
    return result.stdout


def probe(path, opts={}):
    """Probe a media file's duration, bit rate, and per-stream codec/dimensions via ffprobe.

    :param str path: Local media file path.
    :param dict opts: 'ffprobe_bin' (str, default CONFIG["video"]["ffprobe_bin"]),
        'timeout' (float) overrides the default subprocess timeout.
    :returns: {"duration_s": float|None, "bit_rate": int|None, "streams": [{"codec_name":
        str, "codec_type": "video"|"audio", "width": int|None, "height": int|None}, ...]}
    :rtype: dict
    :raises FFmpegError: If `ffprobe` fails or times out.
    """
    ffprobe_bin = opts.get("ffprobe_bin", CONFIG["video"]["ffprobe_bin"])
    argv = [
        ffprobe_bin, "-v", "error",
        "-show_entries", "format=duration,bit_rate:stream=codec_name,codec_type,width,height",
        "-of", "json", path,
    ]
    data = json.loads(_run(argv, opts))
    fmt = data.get("format", {})
    streams = [
        {
            "codec_name": s.get("codec_name"),
            "codec_type": s.get("codec_type"),
            "width": s.get("width"),
            "height": s.get("height"),
        }
        for s in data.get("streams", [])
    ]
    return {
        "duration_s": float(fmt["duration"]) if fmt.get("duration") else None,
        "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
        "streams": streams,
    }


def has_audio_stream(path, opts={}):
    """Whether a media file has at least one audio stream.

    :param str path: Local media file path.
    :param dict opts: 'ffprobe_bin' (str, default CONFIG["video"]["ffprobe_bin"]),
        'timeout' (float) overrides the default subprocess timeout.
    :returns: True if an audio stream is present.
    :rtype: bool
    :raises FFmpegError: If `ffprobe` fails or times out.
    """
    ffprobe_bin = opts.get("ffprobe_bin", CONFIG["video"]["ffprobe_bin"])
    stdout = _run([ffprobe_bin, "-v", "error", "-show_streams", "-select_streams", "a", path], opts)
    return bool(stdout.strip())


def _ensure_audio_track(src_path, tmp_dir, opts={}):
    """Return a path guaranteed to have an audio stream, synthesizing a silent AAC
    track into a new temp file if `src_path` has none.

    Reproduces the reference pipeline's fallback: every HLS variant needs an audio
    stream, so a video with none gets a silent one merged in before transcoding. The
    synthesized file lives in `tmp_dir` (a caller-managed tempfile.TemporaryDirectory()),
    so no separate cleanup is needed here.

    :param str src_path: Local source video path.
    :param str tmp_dir: Local directory to write a synthesized copy into, if needed.
    :param dict opts: Forwarded to has_audio_stream()/_run(). 'ffmpeg_bin' (str, default
        CONFIG["video"]["ffmpeg_bin"]).
    :returns: `src_path` unchanged if it already has audio, else a new local path with
        a silent AAC track merged in.
    :rtype: str
    :raises FFmpegError: If `ffprobe`/`ffmpeg` fails or times out.
    """
    if has_audio_stream(src_path, opts):
        return src_path
    ffmpeg_bin = opts.get("ffmpeg_bin", CONFIG["video"]["ffmpeg_bin"])
    dest = os.path.join(tmp_dir, f"{os.path.splitext(os.path.basename(src_path))[0]}-with-audio.mp4")
    argv = [
        ffmpeg_bin, "-y", "-i", src_path, "-f", "lavfi", "-i", "anullsrc",
        "-c:v", "copy", "-c:a", "aac", "-shortest", dest,
    ]
    _run(argv, opts)
    return dest


# Per-resolution bitrate ladder, ported from the reference convert.sh's BASE_CONFIG +
# per-resolution literals.
_RESOLUTION_LADDER = {
    "360p":  {"bitrate": "800k",  "maxrate": "856k",  "bufsize": "1200k", "audio_bitrate": "96k"},
    "480p":  {"bitrate": "1400k", "maxrate": "1498k", "bufsize": "2100k", "audio_bitrate": "128k"},
    "720p":  {"bitrate": "2800k", "maxrate": "2996k", "bufsize": "4200k", "audio_bitrate": "128k"},
    "1080p": {"bitrate": "5000k", "maxrate": "5350k", "bufsize": "7500k", "audio_bitrate": "192k"},
}

# Master-playlist BANDWIDTH/RESOLUTION values, ported verbatim from the reference's
# static playlist.m3u8 template.
_MASTER_PLAYLIST_LINES = {
    "360p":  "#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360",
    "480p":  "#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=842x480",
    "720p":  "#EXT-X-STREAM-INF:BANDWIDTH=2800000,RESOLUTION=1280x720",
    "1080p": "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080",
}

RESOLUTIONS = tuple(_RESOLUTION_LADDER)


def transcode_hls_rung(src_path, dest_dir, resolution, opts={}):
    """Transcode one HLS resolution variant: an .m3u8 playlist plus a single
    fragmented-MP4 segment file.

    `ffmpeg -i <src> -vf pad=ceil(iw/2)*2:ceil(ih/2)*2 -c:a aac -ar 48000 -c:v h264
    -profile:v main -pix_fmt yuv420p -crf 20 -sc_threshold 0 -g 48 -keyint_min 48
    -hls_flags single_file -hls_time 4 -hls_playlist_type vod -hls_segment_type fmp4
    -b:v {bitrate} -maxrate {maxrate} -bufsize {bufsize} -b:a {audio_bitrate}
    -hls_segment_filename <dest_dir>/<resolution>.mp4 <dest_dir>/<resolution>.m3u8`.
    The pad filter forces even width/height (yuv420p requires it) for odd-dimensioned
    sources.

    :param str src_path: Local source video path — should already be guaranteed an
        audio stream (see _ensure_audio_track()) since every rung needs one.
    :param str dest_dir: Local directory to write the playlist + segment into.
    :param str resolution: One of RESOLUTIONS ("360p", "480p", "720p", "1080p").
    :param dict opts: 'crf' (int, default CONFIG["video"]["crf"]), 'gop_size' (int,
        default CONFIG["video"]["gop_size"]), 'segment_seconds' (int, default
        CONFIG["video"]["segment_seconds"]), 'video_codec' (str, default
        CONFIG["video"]["video_codec"]), 'pix_fmt' (str, default CONFIG["video"]["pix_fmt"]),
        'audio_codec' (str, default CONFIG["video"]["audio_codec"]), 'audio_sample_rate'
        (int, default CONFIG["video"]["audio_sample_rate"]), 'ffmpeg_bin', 'timeout'.
    :returns: {"argv": [...], "playlist_path": str, "segment_path": str}
    :rtype: dict
    :raises ValueError: If `resolution` isn't one of RESOLUTIONS.
    :raises FFmpegError: If `ffmpeg` fails or times out.
    """
    if resolution not in _RESOLUTION_LADDER:
        raise ValueError(f"unknown resolution {resolution!r} (use one of {RESOLUTIONS})")
    rung = _RESOLUTION_LADDER[resolution]
    ffmpeg_bin = opts.get("ffmpeg_bin", CONFIG["video"]["ffmpeg_bin"])
    gop_size = str(opts.get("gop_size", CONFIG["video"]["gop_size"]))
    playlist_path = os.path.join(dest_dir, f"{resolution}.m3u8")
    segment_path = os.path.join(dest_dir, f"{resolution}.mp4")

    argv = [
        ffmpeg_bin, "-y", "-i", src_path,
        "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:a", opts.get("audio_codec", CONFIG["video"]["audio_codec"]),
        "-ar", str(opts.get("audio_sample_rate", CONFIG["video"]["audio_sample_rate"])),
        "-c:v", opts.get("video_codec", CONFIG["video"]["video_codec"]),
        "-profile:v", "main",
        "-pix_fmt", opts.get("pix_fmt", CONFIG["video"]["pix_fmt"]),
        "-crf", str(opts.get("crf", CONFIG["video"]["crf"])),
        "-sc_threshold", "0",
        "-g", gop_size,
        "-keyint_min", gop_size,
        "-hls_flags", "single_file",
        "-hls_time", str(opts.get("segment_seconds", CONFIG["video"]["segment_seconds"])),
        "-hls_playlist_type", "vod",
        "-hls_segment_type", "fmp4",
        "-b:v", rung["bitrate"], "-maxrate", rung["maxrate"], "-bufsize", rung["bufsize"],
        "-b:a", rung["audio_bitrate"],
        "-hls_segment_filename", segment_path,
        playlist_path,
    ]
    _run(argv, opts)
    return {"argv": argv, "playlist_path": playlist_path, "segment_path": segment_path}


def hls_ladder(src_path, dest_dir, resolutions, opts={}):
    """Transcode every requested HLS resolution rung sequentially, then write the
    master playlist unconditionally as the final step.

    Runs the whole ladder as one sequential pass in this process rather than the
    reference pipeline's per-resolution Cloud Run Job fan-out — there's no equivalent
    parallel-task-index mechanism in this Dagster-based architecture, so the master
    playlist no longer needs the reference's "only copy it when generating 1080p,
    because that's presumably the last parallel task to finish" timing-dependent hack;
    it's simply written once every rung is done.

    :param str src_path: Local source video path.
    :param str dest_dir: Local directory to write every rung + the master playlist into.
        Also used as the scratch directory for _ensure_audio_track()'s synthesized copy,
        if the source has no audio stream.
    :param list[str] resolutions: Subset of RESOLUTIONS to generate, in the order the
        master playlist should list them.
    :param dict opts: Forwarded to transcode_hls_rung()/_ensure_audio_track().
    :returns: {"rungs": [transcode_hls_rung() result, ...], "master_playlist_path": str}
    :rtype: dict
    :raises ValueError: If any entry in `resolutions` isn't one of RESOLUTIONS.
    :raises FFmpegError: If any rung's `ffmpeg` invocation fails or times out.
    """
    src_path = _ensure_audio_track(src_path, dest_dir, opts)
    rungs = [transcode_hls_rung(src_path, dest_dir, resolution, opts) for resolution in resolutions]

    master_playlist_path = os.path.join(dest_dir, "playlist.m3u8")
    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
    for resolution in resolutions:
        lines.append(_MASTER_PLAYLIST_LINES[resolution])
        lines.append(f"{resolution}.m3u8")
    with open(master_playlist_path, "w") as f:
        f.write("\n".join(lines) + "\n")

    return {"rungs": rungs, "master_playlist_path": master_playlist_path}


# No reference precedent — picked from the target container's usual codec.
_AUDIO_CODEC_BY_EXTENSION = {
    ".mp3": "libmp3lame", ".aac": "aac", ".m4a": "aac",
    ".ogg": "libvorbis", ".flac": "flac", ".wav": "pcm_s16le",
}


def transcode_audio(src_path, dest_path, opts={}):
    """Transcode an audio (or video) file's audio track to another codec/bitrate.

    `ffmpeg -i <src> -vn -c:a {codec} -b:a {bitrate} -ar {sample_rate} <dest>`.

    :param str src_path: Local source path.
    :param str dest_path: Local destination path — extension picks the default codec
        (see _AUDIO_CODEC_BY_EXTENSION) unless 'codec' is given explicitly.
    :param dict opts: 'codec' (str), 'bitrate' (str, default
        CONFIG["audio"]["transcode_bitrate"]), 'sample_rate' (int, default
        CONFIG["video"]["audio_sample_rate"]), 'ffmpeg_bin', 'timeout'.
    :returns: {"argv": [...], "dest_path": str}
    :rtype: dict
    :raises FFmpegError: If `ffmpeg` fails or times out.
    """
    ffmpeg_bin = opts.get("ffmpeg_bin", CONFIG["video"]["ffmpeg_bin"])
    ext = os.path.splitext(dest_path)[1].lower()
    codec = opts.get("codec", _AUDIO_CODEC_BY_EXTENSION.get(ext, "aac"))
    bitrate = opts.get("bitrate", CONFIG["audio"]["transcode_bitrate"])
    sample_rate = opts.get("sample_rate", CONFIG["video"]["audio_sample_rate"])
    argv = [
        ffmpeg_bin, "-y", "-i", src_path, "-vn",
        "-c:a", codec, "-b:a", bitrate, "-ar", str(sample_rate),
        dest_path,
    ]
    _run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}


def waveform(src_path, dest_path, opts={}):
    """Render a waveform image from an audio (or video) file's audio track.

    `ffmpeg -i <src> -filter_complex showwavespic=s={width}x{height}:colors={color}
    -frames:v 1 -update 1 <dest>`.

    :param str src_path: Local source path.
    :param str dest_path: Local destination image path.
    :param dict opts: 'width' (int, default CONFIG["audio"]["waveform_width"]), 'height'
        (int, default CONFIG["audio"]["waveform_height"]), 'color' (str, default
        CONFIG["audio"]["waveform_color"]), 'ffmpeg_bin', 'timeout'.
    :returns: {"argv": [...], "dest_path": str}
    :rtype: dict
    :raises FFmpegError: If `ffmpeg` fails or times out.
    """
    ffmpeg_bin = opts.get("ffmpeg_bin", CONFIG["video"]["ffmpeg_bin"])
    width = opts.get("width", CONFIG["audio"]["waveform_width"])
    height = opts.get("height", CONFIG["audio"]["waveform_height"])
    color = opts.get("color", CONFIG["audio"]["waveform_color"])
    argv = [
        ffmpeg_bin, "-y", "-i", src_path,
        "-filter_complex", f"showwavespic=s={width}x{height}:colors={color}",
        "-frames:v", "1", "-update", "1", dest_path,
    ]
    _run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}
