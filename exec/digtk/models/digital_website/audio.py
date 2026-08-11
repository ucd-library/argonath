"""
Audio item derivative-generation workflow.

Structurally parallel to digtk.models.digital_website.image, built on the same shared
digtk.models.digital_website.pipeline staging/publishing/lineage plumbing, using
digtk.lib.ffmpeg_utils for the actual transcoding/waveform/probing work. Both outputs
(transcoded audio, waveform image) are "a rendered derivative of the source" and land in
the item's ordinary 'derivatives/' silver subdirectory, same as image thumbnails.

Each source audio file gets one AudioObject associatedMedia entry, tagged with its own
filename stem as 'identifier' so a later `digtk captions transcribe` run (see
digtk.models.digital_website.captions) can attach its caption to the right entry.
"""
import os
import tempfile
import time

from digtk.lib import ffmpeg_utils, items, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models.digital_website import pipeline

_LINK_RELATION = f"{ARGONATH_NS}derivativeOf"


def probe(ark, opts={}):
    """Report ffprobe metadata for every source audio file in the item — ad hoc/debug,
    no derivatives generated.

    :param str ark: Item ARK identifier.
    :param dict opts: Unused.
    :returns: {"status": "success", "ark": str, "streams": {stem: ffmpeg_utils.probe() result}}
    :rtype: dict
    """
    item = items.resolve(ark)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        streams = {src["stem"]: ffmpeg_utils.probe(src["local_path"], opts) for src in sources}
    return {"status": "success", "ark": ark, "streams": streams}


def transcode(ark, opts={}):
    """Transcode every source audio file's to another format.

    Ad hoc/manual use, mirroring digtk.models.digital_website.image.convert(); derivatives()
    is what Dagster's derivatives_job actually calls.

    :param str ark: Item ARK identifier.
    :param dict opts: 'format' (str, default CONFIG["audio"]["transcode_format"]),
        'bitrate' (str, default CONFIG["audio"]["transcode_bitrate"]).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    fmt = opts.get("format", CONFIG["audio"]["transcode_format"])
    item = items.resolve(ark)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    start = time.monotonic()
    multi = len(audio_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        local_outputs = []
        for src in sources:
            filename = pipeline.output_name(src["stem"], f"converted.{fmt}", multi)
            local_dest = os.path.join(tmp_dir, filename)
            ffmpeg_utils.transcode_audio(src["local_path"], local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, "derivatives", local_outputs,
            command=["digtk", "audio", "transcode", ark, "--format", fmt],
            params={"format": fmt},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION,
            },
        )

    return {"status": "success", "ark": ark, **result}


def waveform(ark, opts={}):
    """Generate a waveform image for every source audio file in the item.

    Ad hoc/manual use.

    :param str ark: Item ARK identifier.
    :param dict opts: 'width' (int, default CONFIG["audio"]["waveform_width"]), 'height'
        (int, default CONFIG["audio"]["waveform_height"]), 'color' (str, default
        CONFIG["audio"]["waveform_color"]).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    item = items.resolve(ark)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    start = time.monotonic()
    multi = len(audio_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        local_outputs = []
        for src in sources:
            width = opts.get("width", CONFIG["audio"]["waveform_width"])
            height = opts.get("height", CONFIG["audio"]["waveform_height"])
            filename = pipeline.output_name(src["stem"], f"waveform-{width}x{height}.png", multi)
            local_dest = os.path.join(tmp_dir, filename)
            ffmpeg_utils.waveform(src["local_path"], local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))

        result = pipeline.publish(
            item, "derivatives", local_outputs,
            command=["digtk", "audio", "waveform", ark],
            params={k: v for k, v in opts.items() if k in ("width", "height", "color")},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION,
            },
        )

    return {"status": "success", "ark": ark, **result}


def derivatives(ark, opts={}):
    """Generate the full derivative set for every source audio file in the item —
    the umbrella `digtk audio derivatives` calls: transcode + waveform.

    :param str ark: Item ARK identifier.
    :param dict opts: 'format' (str, default CONFIG["audio"]["transcode_format"]),
        'width'/'height' (waveform dimensions); forwarded to ffmpeg_utils.
    :returns: {"status": "success", "ark": str, "derivatives": {...}}
    :rtype: dict
    """
    fmt = opts.get("format", CONFIG["audio"]["transcode_format"])
    item = items.resolve(ark)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    start = time.monotonic()
    multi = len(audio_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        total = len(sources)
        local_outputs = []
        converted_filename_by_stem = {}

        for i, src in enumerate(sources, start=1):
            stem = src["stem"]
            utils.log(f"[{i}/{total}] {stem}: transcoding to {fmt}...")
            converted_filename = pipeline.output_name(stem, f"converted.{fmt}", multi)
            converted_path = os.path.join(tmp_dir, converted_filename)
            ffmpeg_utils.transcode_audio(src["local_path"], converted_path, opts)
            local_outputs.append((converted_filename, converted_path, src["bronze_path"]))
            converted_filename_by_stem[stem] = converted_filename

            utils.log(f"[{i}/{total}] {stem}: generating waveform...")
            width = opts.get("width", CONFIG["audio"]["waveform_width"])
            height = opts.get("height", CONFIG["audio"]["waveform_height"])
            waveform_filename = pipeline.output_name(stem, f"waveform-{width}x{height}.png", multi)
            waveform_path = os.path.join(tmp_dir, waveform_filename)
            ffmpeg_utils.waveform(src["local_path"], waveform_path, opts)
            local_outputs.append((waveform_filename, waveform_path, src["bronze_path"]))
            utils.log(f"[{i}/{total}] {stem}: done")

        duration_s = time.monotonic() - start
        result = pipeline.publish(
            item, "derivatives", local_outputs,
            command=["digtk", "audio", "derivatives", ark],
            params={"format": fmt},
            opts={"manifest_opts": {"duration_s": duration_s}, "link_relation": _LINK_RELATION},
        )

        utils.log("Updating associated-media sidecar...")
        audio_objects = [
            {
                "@type": "AudioObject",
                "identifier": src["stem"],
                "contentUrl": pipeline.cask_public_ref(f"{result['dir']}/{converted_filename_by_stem[src['stem']]}"),
                "encodingFormat": f"audio/{fmt}",
            }
            for src in sources
        ]
        pipeline.update_associated_media(item, audio_objects, [src["bronze_path"] for src in sources], "audio")

    return {"status": "success", "ark": ark, "derivatives": result}
