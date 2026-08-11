"""
Video item derivative-generation workflow.

Structurally parallel to digtk.models.digital_website.image, built on the same shared
digtk.models.digital_website.pipeline staging/publishing/lineage plumbing, using
digtk.lib.ffmpeg_utils for the actual transcoding/probing work. A source video's HLS
stream (master playlist + every resolution rung — many small files) is published under
its own 'stream/' silver subdirectory per item, distinct from 'derivatives/' (which here
holds the poster frame — a single rendered still, same conceptual bucket as an image
thumbnail).

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

_LINK_RELATION = {
    "derivatives": f"{ARGONATH_NS}derivativeOf",
    "stream": f"{ARGONATH_NS}streamOf",
}


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
    is what Dagster's derivatives_job actually calls.

    :param str ark: Item ARK identifier.
    :param dict opts: 'resolutions' (comma-separated str, default
        CONFIG["video"]["resolutions"]); forwarded to ffmpeg_utils.hls_ladder().
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    resolutions = _parse_resolutions(opts.get("resolutions", CONFIG["video"]["resolutions"]))
    item = items.resolve(ark)
    video_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video")
    start = time.monotonic()
    multi = len(video_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": video_paths})
        local_outputs = []
        for src in sources:
            stem = src["stem"]
            stage_dir = os.path.join(tmp_dir, f"{stem}-stream")
            os.makedirs(stage_dir, exist_ok=True)
            ladder = ffmpeg_utils.hls_ladder(src["local_path"], stage_dir, resolutions, opts)
            for rung in ladder["rungs"]:
                for path in (rung["playlist_path"], rung["segment_path"]):
                    filename = pipeline.output_name(stem, os.path.basename(path), multi)
                    local_outputs.append((filename, path, src["bronze_path"]))
            master_filename = pipeline.output_name(stem, "playlist.m3u8", multi)
            local_outputs.append((master_filename, ladder["master_playlist_path"], src["bronze_path"]))

        result = pipeline.publish(
            item, "stream", local_outputs,
            command=["digtk", "video", "hls", ark, "--resolutions", ",".join(resolutions)],
            params={"resolutions": resolutions},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION["stream"],
            },
        )

    return {"status": "success", "ark": ark, **result}


def poster(ark, opts={}):
    """Extract a poster-frame JPEG for every source video in the item.

    Ad hoc/manual use; derivatives() is what Dagster's derivatives_job actually calls.

    :param str ark: Item ARK identifier.
    :param dict opts: 'timestamp_s' (float, default CONFIG["video"]["poster_timestamp_s"]),
        'width' (int, default CONFIG["video"]["poster_width"]).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    item = items.resolve(ark)
    video_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video")
    start = time.monotonic()
    multi = len(video_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": video_paths})
        local_outputs = []
        for src in sources:
            width = opts.get("width", CONFIG["video"]["poster_width"])
            filename = pipeline.output_name(src["stem"], f"poster-{width}.jpg", multi)
            local_dest = os.path.join(tmp_dir, filename)
            ffmpeg_utils.extract_poster(src["local_path"], local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, "derivatives", local_outputs,
            command=["digtk", "video", "poster", ark],
            params={k: v for k, v in opts.items() if k in ("timestamp_s", "width")},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION["derivatives"],
            },
        )

    return {"status": "success", "ark": ark, **result}


def derivatives(ark, opts={}):
    """Generate the full derivative set for every source video in the item — the
    umbrella `digtk video derivatives` calls.

    Runs the HLS adaptive-bitrate ladder and extracts a poster frame for every source
    video, publishing the stream to 'stream/' and the poster to 'derivatives/', then
    transcribes every source to a WebVTT caption via captions.transcribe_sources()
    (local Whisper ASR — machine-generated and unreviewed, see
    docs/cask-conventions.md's Captions/Transcripts section) using the same staged
    sources, merging each caption into the VideoObject entry update_associated_media()
    just wrote. All local generation happens before anything is published to CaskFS,
    so a failure at any step leaves no partial derivative set behind.

    :param str ark: Item ARK identifier.
    :param dict opts: 'resolutions' (comma-separated str, default
        CONFIG["video"]["resolutions"]), 'width' (int, poster width); 'language'/'model'
        for the caption step (see captions.transcribe_sources()); forwarded to
        ffmpeg_utils.
    :returns: {"status": "success", "ark": str, "stream": {...}, "derivatives": {...},
        "captions": {...}}
    :rtype: dict
    """
    resolutions = _parse_resolutions(opts.get("resolutions", CONFIG["video"]["resolutions"]))
    item = items.resolve(ark)
    video_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video")
    start = time.monotonic()
    multi = len(video_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": video_paths})
        total = len(sources)
        stream_outputs = []
        poster_outputs_by_stem = {}

        for i, src in enumerate(sources, start=1):
            stem = src["stem"]
            utils.log(f"[{i}/{total}] {stem}: transcoding HLS ladder ({', '.join(resolutions)})...")
            stage_dir = os.path.join(tmp_dir, f"{stem}-stream")
            os.makedirs(stage_dir, exist_ok=True)
            ladder = ffmpeg_utils.hls_ladder(src["local_path"], stage_dir, resolutions, opts)
            for rung in ladder["rungs"]:
                for path in (rung["playlist_path"], rung["segment_path"]):
                    filename = pipeline.output_name(stem, os.path.basename(path), multi)
                    stream_outputs.append((filename, path, src["bronze_path"]))
            master_filename = pipeline.output_name(stem, "playlist.m3u8", multi)
            stream_outputs.append((master_filename, ladder["master_playlist_path"], src["bronze_path"]))

            utils.log(f"[{i}/{total}] {stem}: extracting poster frame...")
            width = opts.get("width", CONFIG["video"]["poster_width"])
            poster_filename = pipeline.output_name(stem, f"poster-{width}.jpg", multi)
            poster_path = os.path.join(tmp_dir, poster_filename)
            ffmpeg_utils.extract_poster(src["local_path"], poster_path, opts)
            poster_outputs_by_stem[stem] = (poster_filename, poster_path, src["bronze_path"])
            utils.log(f"[{i}/{total}] {stem}: done")

        duration_s = time.monotonic() - start
        stream_result = pipeline.publish(
            item, "stream", stream_outputs,
            command=["digtk", "video", "derivatives", ark, "--resolutions", ",".join(resolutions)],
            params={"resolutions": resolutions},
            opts={"manifest_opts": {"duration_s": duration_s}, "link_relation": _LINK_RELATION["stream"]},
        )
        derivatives_result = pipeline.publish(
            item, "derivatives", list(poster_outputs_by_stem.values()),
            command=["digtk", "video", "derivatives", ark],
            params={},
            opts={"manifest_opts": {"duration_s": duration_s}, "link_relation": _LINK_RELATION["derivatives"]},
        )

        utils.log("Updating associated-media sidecar...")
        video_objects = []
        for src in sources:
            stem = src["stem"]
            master_filename = pipeline.output_name(stem, "playlist.m3u8", multi)
            poster_filename = poster_outputs_by_stem[stem][0]
            video_objects.append({
                "@type": "VideoObject",
                "identifier": stem,
                "contentUrl": pipeline.cask_public_ref(f"{stream_result['dir']}/{master_filename}"),
                "encodingFormat": "application/vnd.apple.mpegurl",
                "thumbnailUrl": pipeline.cask_public_ref(f"{derivatives_result['dir']}/{poster_filename}"),
            })
        pipeline.update_associated_media(item, video_objects, [src["bronze_path"] for src in sources], "video")

        utils.log("Transcribing captions...")
        captions_result = captions.transcribe_sources(
            item, sources, tmp_dir,
            command=["digtk", "video", "derivatives", ark, "--resolutions", ",".join(resolutions)],
            opts=opts,
        )

    return {
        "status": "success", "ark": ark,
        "stream": stream_result, "derivatives": derivatives_result, "captions": captions_result,
    }
