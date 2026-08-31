"""
Video item derivative-generation workflow.

Structurally parallel to digtk.models.digital_website.image, built on the same shared
digtk.models.digital_website.pipeline staging/publishing/lineage plumbing, using
digtk.lib.ffmpeg_utils for the actual transcoding/probing work. A source video's HLS
stream (master playlist + every resolution rung — many small files) publishes into its
own directory under PUBLIC_FRONTEND_DIR, named after its own bronze filename (e.g.
`public-frontend-derivatives/movie.mp4/streaming/`) — same disambiguation-for-free
pattern digtk.models.digital_website.pdf uses for a PDF's own directory.

This module only ever generates the stream itself — a video item's thumbnail, if it
needs one, comes from a companion image bronze binary (supplied directly by the
archiving team, e.g. a `.jpg` alongside the `.mp4`) processed independently by
`digtk public-dc image derivatives`, under the ordinary Multi-Format Items convention (see
docs/cask-conventions.md) — there's no ffmpeg-extracted poster frame here.

Each source video gets one VideoObject associatedMedia entry, tagged with its own
filename stem as 'identifier' so derivatives()'s own captions.transcribe_sources()
call (see digtk.models.digital_website.captions) can attach its caption to the
right entry without needing to re-derive which video it belongs to.
"""
import os
import tempfile
import time

from digtk.lib import ffmpeg_utils, items, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models.digital_website import captions, pipeline

_LINK_RELATION = f"{ARGONATH_NS}streamOf"


def _parse_resolutions(resolutions):
    """Parse a resolution-ladder spec into a list of rung names.

    :param str|list[str] resolutions: Comma-separated string (e.g. "360p,720p"), or
        already a list.
    :rtype: list[str]
    """
    if isinstance(resolutions, str):
        resolutions = [r.strip() for r in resolutions.split(",")]
    return resolutions


def probe(ark, opts={}):
    """Report ffprobe metadata for every source video in the item — ad hoc/debug,
    no derivatives generated.

    :param str ark: Item ARK identifier.
    :param dict opts: Unused.
    :returns: {"status": "success", "ark": str, "streams": {stem: ffmpeg_utils.probe() result}}
    :rtype: dict
    """
    item = items.resolve(ark)
    video_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video")
    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": video_paths})
        streams = {src["stem"]: ffmpeg_utils.probe(src["local_path"], opts) for src in sources}
    return {"status": "success", "ark": ark, "streams": streams}


def hls(ark, opts={}):
    """Generate the HLS adaptive-bitrate stream for every source video in the item.

    Ad hoc/manual use, mirroring digtk.models.digital_website.image.thumbnails(); derivatives()
    is what Dagster's derivatives_job actually calls. Publishes into the same location
    derivatives() would: `public-frontend-derivatives/<video-filename>/streaming/`.

    :param str ark: Item ARK identifier.
    :param dict opts: 'resolutions' (comma-separated str, default
        CONFIG["video"]["resolutions"]); forwarded to ffmpeg_utils.hls_ladder().
    :returns: {"status": "success", "ark": str, "videos": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    resolutions = _parse_resolutions(opts.get("resolutions", CONFIG["video"]["resolutions"]))
    item = items.resolve(ark)
    video_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video")
    start = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": video_paths})
        videos = {}
        for src in sources:
            stem = src["stem"]
            filename = os.path.basename(src["bronze_path"])
            stage_dir = os.path.join(tmp_dir, f"{stem}-stream")
            os.makedirs(stage_dir, exist_ok=True)
            ladder = ffmpeg_utils.hls_ladder(src["local_path"], stage_dir, resolutions, opts)
            local_outputs = []
            for rung in ladder["rungs"]:
                for path in (rung["playlist_path"], rung["segment_path"]):
                    local_outputs.append((os.path.basename(path), path, src["bronze_path"]))
            local_outputs.append(("playlist.m3u8", ladder["master_playlist_path"], src["bronze_path"]))

            videos[filename] = pipeline.publish(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/streaming", local_outputs,
                command=["digtk", "video", "hls", ark, "--resolutions", ",".join(resolutions)],
                params={"resolutions": resolutions},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )

    return {"status": "success", "ark": ark, "videos": videos}


def derivatives(ark, opts={}):
    """Generate the full derivative set for every source video in the item — the
    umbrella `digtk public-dc video derivatives` calls.

    Runs the HLS adaptive-bitrate ladder for every source video, publishing into its
    own `<video-filename>/streaming/` directory (see PUBLIC_FRONTEND_DIR and
    docs/cask-conventions.md's Video Streaming section), then transcribes every
    source to a WebVTT caption via captions.transcribe_sources() (local Whisper ASR —
    machine-generated and unreviewed, see docs/cask-conventions.md's
    Captions/Transcripts section) using the same staged sources, merging each caption
    into the VideoObject entry update_associated_media() just wrote. All local
    generation happens before anything is published to CaskFS, so a failure at any
    step leaves no partial derivative set behind.

    :param str ark: Item ARK identifier.
    :param dict opts: 'resolutions' (comma-separated str, default
        CONFIG["video"]["resolutions"]); 'language'/'model' for the caption step (see
        captions.transcribe_sources()); forwarded to ffmpeg_utils.
    :returns: {"status": "success", "ark": str, "stream": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}, "captions": {...}}
    :rtype: dict
    """
    resolutions = _parse_resolutions(opts.get("resolutions", CONFIG["video"]["resolutions"]))
    item = items.resolve(ark)
    video_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video")
    start = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": video_paths})
        total = len(sources)
        stream_results = {}

        for i, src in enumerate(sources, start=1):
            stem = src["stem"]
            filename = os.path.basename(src["bronze_path"])
            utils.log(f"[{i}/{total}] {stem}: transcoding HLS ladder ({', '.join(resolutions)})...")
            stage_dir = os.path.join(tmp_dir, f"{stem}-stream")
            os.makedirs(stage_dir, exist_ok=True)
            ladder = ffmpeg_utils.hls_ladder(src["local_path"], stage_dir, resolutions, opts)
            stream_outputs = []
            for rung in ladder["rungs"]:
                for path in (rung["playlist_path"], rung["segment_path"]):
                    stream_outputs.append((os.path.basename(path), path, src["bronze_path"]))
            stream_outputs.append(("playlist.m3u8", ladder["master_playlist_path"], src["bronze_path"]))

            stream_results[filename] = pipeline.publish(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/streaming", stream_outputs,
                command=["digtk", "video", "derivatives", ark, "--resolutions", ",".join(resolutions)],
                params={"resolutions": resolutions},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )
            utils.log(f"[{i}/{total}] {stem}: done")

        utils.log("Updating associated-media sidecar...")
        video_objects = []
        for src in sources:
            stem = src["stem"]
            filename = os.path.basename(src["bronze_path"])
            video_objects.append({
                "@type": "VideoObject",
                "identifier": stem,
                "contentUrl": pipeline.cask_public_ref(f"{stream_results[filename]['dir']}/playlist.m3u8"),
                "encodingFormat": "application/vnd.apple.mpegurl",
            })
        pipeline.update_associated_media(item, video_objects, [src["bronze_path"] for src in sources], "video")

        utils.log("Transcribing captions...")
        captions_result = captions.transcribe_sources(
            item, sources, tmp_dir,
            command=["digtk", "video", "derivatives", ark, "--resolutions", ",".join(resolutions)],
            opts=opts,
        )

    return {"status": "success", "ark": ark, "stream": stream_results, "captions": captions_result}
