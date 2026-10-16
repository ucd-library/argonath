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
`digtk showcase image derivatives`, under the ordinary Multi-Format Items convention (see
docs/cask-conventions.md) — there's no ffmpeg-extracted poster frame here.

Each source video gets one VideoObject associatedMedia entry in the item's gold
record (see media_objects()), tagged with its own filename stem as 'identifier' so
digtk.models.digital_website.captions.attach_captions() can attach its caption to
the right entry.
"""
import json
import os
import tempfile
import time

from digtk.lib import ffmpeg_utils, fs, items, utils
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


def probe(target, opts={}):
    """Report ffprobe metadata for every source video in the item — ad hoc/debug,
    no derivatives generated.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Unused.
    :returns: {"status": "success", "item": str (item path), "streams": {stem: ffmpeg_utils.probe() result}}
    :rtype: dict
    """
    item = items.resolve_target(target)
    video_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video")
    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": video_paths})
        streams = {src["stem"]: ffmpeg_utils.probe(src["local_path"], opts) for src in sources}
    return {"status": "success", "item": items.item_id(item), "streams": streams}


def hls(target, opts={}):
    """Generate the HLS adaptive-bitrate stream for every source video in the item.

    Ad hoc/manual use, mirroring digtk.models.digital_website.image.thumbnails(); derivatives()
    is what Dagster's derivatives_job actually calls. Publishes into the same location
    derivatives() would: `public-frontend-derivatives/<video-filename>/streaming/`.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'resolutions' (comma-separated str, default
        CONFIG["video"]["resolutions"]); forwarded to ffmpeg_utils.hls_ladder().
    :returns: {"status": "success", "item": str (item path), "videos": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    resolutions = _parse_resolutions(opts.get("resolutions", CONFIG["video"]["resolutions"]))
    item = items.resolve_target(target)
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
                command=["digtk", "video", "hls", items.item_id(item), "--resolutions", ",".join(resolutions)],
                params={"resolutions": resolutions},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )

    return {"status": "success", "item": items.item_id(item), "videos": videos}


def derivatives(target, opts={}):
    """Generate the full derivative set for every source video in the item — the
    umbrella `digtk showcase video derivatives` calls.

    Runs the HLS adaptive-bitrate ladder for every source video, publishing into its
    own `<video-filename>/streaming/` directory (see PUBLIC_FRONTEND_DIR and
    docs/cask-conventions.md's Video Streaming section), then transcribes every
    source to a WebVTT caption via captions.transcribe_sources() (local Whisper ASR —
    machine-generated and unreviewed, see docs/cask-conventions.md's
    Captions/Transcripts section) using the same staged sources. All local
    generation happens before anything is published to CaskFS, so a failure at any
    step leaves no partial derivative set behind.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'resolutions' (comma-separated str, default
        CONFIG["video"]["resolutions"]); 'language'/'model' for the caption step (see
        captions.transcribe_sources()); forwarded to ffmpeg_utils.
    :returns: {"status": "success", "item": str (item path), "stream": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}, "captions": {...}}
    :rtype: dict
    """
    resolutions = _parse_resolutions(opts.get("resolutions", CONFIG["video"]["resolutions"]))
    item = items.resolve_target(target)
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
                command=["digtk", "video", "derivatives", items.item_id(item), "--resolutions", ",".join(resolutions)],
                params={"resolutions": resolutions},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )
            utils.log(f"[{i}/{total}] {stem}: done")

        utils.log("Transcribing captions...")
        captions_result = captions.transcribe_sources(
            item, sources, tmp_dir,
            command=["digtk", "video", "derivatives", items.item_id(item), "--resolutions", ",".join(resolutions)],
            opts=opts,
        )

    return {"status": "success", "item": items.item_id(item), "stream": stream_results, "captions": captions_result}


def media_objects(item):
    """The item's "video" associatedMedia entries — one VideoObject per source
    video, pointing at its published HLS master playlist — rebuilt from each
    video's streaming/manifest.json. Read by
    digtk.models.digital_website.item_record.build().

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: VideoObject entries, each tagged with its source's filename stem
        as 'identifier' (see captions.attach_captions()).
    :rtype: list[dict]
    :raises FileNotFoundError: If a video's stream hasn't been published.
    """
    objects = []
    for path in pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video"):
        filename = os.path.basename(path)
        stream_dir = pipeline.derivative_dir(item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/streaming")
        manifest = json.loads(fs.read(f"{stream_dir}/manifest.json"))
        if "playlist.m3u8" not in manifest.get("outputs", []):
            raise FileNotFoundError(f"no HLS playlist published for {filename} of {items.item_id(item)}")
        objects.append({
            "@type": "VideoObject",
            "identifier": os.path.splitext(filename)[0],
            "contentUrl": pipeline.cask_public_ref(f"{stream_dir}/playlist.m3u8"),
            "encodingFormat": "application/vnd.apple.mpegurl",
        })
    return objects
