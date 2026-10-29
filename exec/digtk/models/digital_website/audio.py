"""
Audio item derivative-generation workflow.

Structurally parallel to digtk.models.digital_website.image, built on the same shared
digtk.models.digital_website.pipeline staging/publishing/lineage plumbing, using
digtk.lib.ffmpeg_utils for the actual transcoding/waveform/probing work. Each source
audio file's transcoded copy + waveform image publish together into their own
directory under PUBLIC_FRONTEND_DIR, named after the source's own bronze filename
(same disambiguation-for-free pattern digtk.models.digital_website.pdf/video use) —
flat inside, with no further nested content-kind subfolder, since this is the only
kind of output audio produces here.

Each source audio file gets one AudioObject associatedMedia entry in the item's
showcase search document (see media_objects()), tagged with its own filename stem as 'identifier' so
digtk.models.digital_website.captions.attach_captions() can attach its caption to the
right entry. Captions themselves are unaffected by this module's layout — they still
publish into their own flat, shared 'captions/' directory.
"""
import json
import os
import tempfile
import time

from digtk.lib import ffmpeg_utils, fs, items, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models.digital_website import captions, pipeline

_LINK_RELATION = f"{ARGONATH_NS}derivativeOf"


def probe(target, opts={}):
    """Report ffprobe metadata for every source audio file in the item — ad hoc/debug,
    no derivatives generated.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: Unused.
    :returns: {"status": "success", "item": str (item path), "streams": {stem: ffmpeg_utils.probe() result}}
    :rtype: dict
    """
    item = items.resolve_target(target)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        streams = {src["stem"]: ffmpeg_utils.probe(src["local_path"], opts) for src in sources}
    return {"status": "success", "item": items.item_id(item), "streams": streams}


def transcode(target, opts={}):
    """Transcode every source audio file to another format.

    Ad hoc/manual use, mirroring digtk.models.digital_website.image.convert(); derivatives()
    is what Dagster's showcase_derivatives_job actually calls. Publishes into the same location
    derivatives() would: `public-frontend-derivatives/<filename>/`. Note this overwrites
    that directory's manifest with one describing only the converted file — run
    derivatives() afterward to restore a manifest covering the full set if that matters.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'format' (str, default CONFIG["audio"]["transcode_format"]),
        'bitrate' (str, default CONFIG["audio"]["transcode_bitrate"]).
    :returns: {"status": "success", "item": str (item path), "audios": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    fmt = opts.get("format", CONFIG["audio"]["transcode_format"])
    item = items.resolve_target(target)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    start = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        audios = {}
        for src in sources:
            filename = os.path.basename(src["bronze_path"])
            converted_filename = f"converted.{fmt}"
            local_dest = os.path.join(tmp_dir, f"{src['stem']}-{converted_filename}")
            ffmpeg_utils.transcode_audio(src["local_path"], local_dest, opts=opts)
            local_outputs = [(converted_filename, local_dest, src["bronze_path"])]

            audios[filename] = pipeline.publish(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}", local_outputs,
                command=["digtk", "audio", "transcode", items.item_id(item), "--format", fmt],
                params={"format": fmt},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )

    return {"status": "success", "item": items.item_id(item), "audios": audios}


def waveform(target, opts={}):
    """Generate a waveform image for every source audio file in the item.

    Ad hoc/manual use. Publishes into the same location derivatives() would:
    `public-frontend-derivatives/<filename>/`. Note this overwrites that directory's
    manifest with one describing only the waveform — run derivatives() afterward to
    restore a manifest covering the full set if that matters.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'width' (int, default CONFIG["audio"]["waveform_width"]), 'height'
        (int, default CONFIG["audio"]["waveform_height"]), 'color' (str, default
        CONFIG["audio"]["waveform_color"]).
    :returns: {"status": "success", "item": str (item path), "audios": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    item = items.resolve_target(target)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    start = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        audios = {}
        for src in sources:
            filename = os.path.basename(src["bronze_path"])
            width = opts.get("width", CONFIG["audio"]["waveform_width"])
            height = opts.get("height", CONFIG["audio"]["waveform_height"])
            waveform_filename = f"waveform-{width}x{height}.png"
            local_dest = os.path.join(tmp_dir, f"{src['stem']}-{waveform_filename}")
            ffmpeg_utils.waveform(src["local_path"], local_dest, opts=opts)
            local_outputs = [(waveform_filename, local_dest, src["bronze_path"])]

            audios[filename] = pipeline.publish(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}", local_outputs,
                command=["digtk", "audio", "waveform", items.item_id(item)],
                params={k: v for k, v in opts.items() if k in ("width", "height", "color")},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )

    return {"status": "success", "item": items.item_id(item), "audios": audios}


def derivatives(target, opts={}):
    """Generate the full derivative set for every source audio file in the item —
    the umbrella `digtk showcase audio derivatives` calls: transcode + waveform together into
    one directory per source, then transcribes every source to a WebVTT caption via
    captions.transcribe_sources() (local Whisper ASR — machine-generated and
    unreviewed, see docs/cask-conventions.md's Captions/Transcripts section) using the
    same staged sources. Captions still publish into their own flat, shared
    'captions/' directory, unaffected by this per-source layout.

    :param str|digtk.lib.items.ItemLocation target: Item ARK, bronze item folder,
        or an already-resolved location — see digtk.lib.items.resolve_target().
    :param dict opts: 'format' (str, default CONFIG["audio"]["transcode_format"]),
        'width'/'height' (waveform dimensions); 'language'/'model' for the caption
        step (see captions.transcribe_sources()); forwarded to ffmpeg_utils.
    :returns: {"status": "success", "item": str (item path), "derivatives": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}, "captions": {...}}
    :rtype: dict
    """
    fmt = opts.get("format", CONFIG["audio"]["transcode_format"])
    item = items.resolve_target(target)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    start = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        total = len(sources)
        derivatives_results = {}

        for i, src in enumerate(sources, start=1):
            stem = src["stem"]
            filename = os.path.basename(src["bronze_path"])
            utils.log(f"[{i}/{total}] {stem}: transcoding to {fmt}...")
            converted_filename = f"converted.{fmt}"
            converted_path = os.path.join(tmp_dir, f"{stem}-{converted_filename}")
            ffmpeg_utils.transcode_audio(src["local_path"], converted_path, opts)
            local_outputs = [(converted_filename, converted_path, src["bronze_path"])]

            utils.log(f"[{i}/{total}] {stem}: generating waveform...")
            width = opts.get("width", CONFIG["audio"]["waveform_width"])
            height = opts.get("height", CONFIG["audio"]["waveform_height"])
            waveform_filename = f"waveform-{width}x{height}.png"
            waveform_path = os.path.join(tmp_dir, f"{stem}-{waveform_filename}")
            ffmpeg_utils.waveform(src["local_path"], waveform_path, opts)
            local_outputs.append((waveform_filename, waveform_path, src["bronze_path"]))

            derivatives_results[filename] = pipeline.publish(
                item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}", local_outputs,
                command=["digtk", "audio", "derivatives", items.item_id(item)],
                params={"format": fmt},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )
            utils.log(f"[{i}/{total}] {stem}: done")

        utils.log("Transcribing captions...")
        captions_result = captions.transcribe_sources(
            item, sources, tmp_dir,
            command=["digtk", "audio", "derivatives", items.item_id(item)],
            opts=opts,
        )

    return {"status": "success", "item": items.item_id(item), "derivatives": derivatives_results, "captions": captions_result}


def media_objects(item):
    """The item's "audio" associatedMedia entries — one AudioObject per source
    audio file, pointing at its published transcoded copy — rebuilt from each
    source's manifest.json. Read by
    digtk.lib.showcase_index._silver_media().

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :returns: AudioObject entries, each tagged with its source's filename stem
        as 'identifier' (see captions.attach_captions()).
    :rtype: list[dict]
    :raises FileNotFoundError: If a source's transcoded copy hasn't been published.
    """
    objects = []
    for path in pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio"):
        filename = os.path.basename(path)
        audio_dir = pipeline.derivative_dir(item, f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}")
        manifest = json.loads(fs.read(f"{audio_dir}/manifest.json"))
        converted = next((o for o in manifest.get("outputs", []) if o.startswith("converted.")), None)
        if not converted:
            raise FileNotFoundError(f"no transcoded audio published for {filename} of {items.item_id(item)}")
        objects.append({
            "@type": "AudioObject",
            "identifier": os.path.splitext(filename)[0],
            "contentUrl": pipeline.cask_public_ref(f"{audio_dir}/{converted}"),
            "encodingFormat": f"audio/{converted.split('.', 1)[1]}",
        })
    return objects
