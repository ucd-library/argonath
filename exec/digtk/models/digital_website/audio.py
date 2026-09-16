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

Each source audio file gets one AudioObject associatedMedia entry, tagged with its own
filename stem as 'identifier' so derivatives()'s own captions.transcribe_sources()
call (see digtk.models.digital_website.captions) can attach its caption to the right
entry. Captions themselves are unaffected by this module's layout — they still
publish into their own flat, shared 'captions/' directory.
"""
import os
import tempfile
import time

from digtk.lib import ffmpeg_utils, items, utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models.digital_website import captions, pipeline

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
    """Transcode every source audio file to another format.

    Ad hoc/manual use, mirroring digtk.models.digital_website.image.convert(); derivatives()
    is what Dagster's derivatives_job actually calls. Publishes into the same location
    derivatives() would: `public-frontend-derivatives/<filename>/`. Note this overwrites
    that directory's manifest with one describing only the converted file — run
    derivatives() afterward to restore a manifest covering the full set if that matters.

    :param str ark: Item ARK identifier.
    :param dict opts: 'format' (str, default CONFIG["audio"]["transcode_format"]),
        'bitrate' (str, default CONFIG["audio"]["transcode_bitrate"]), 'app' (str)
        — target gold app, this transcode is a final public-facing product with
        no silver staging.
    :returns: {"status": "success", "ark": str, "audios": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    fmt = opts.get("format", CONFIG["audio"]["transcode_format"])
    app = opts.get("app")
    item = items.resolve(ark)
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
                command=["digtk", "audio", "transcode", ark, "--format", fmt],
                params={"format": fmt},
                type="gold", type_opts={"app": app},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )

    return {"status": "success", "ark": ark, "audios": audios}


def waveform(ark, opts={}):
    """Generate a waveform image for every source audio file in the item.

    Ad hoc/manual use. Publishes into the same location derivatives() would:
    `public-frontend-derivatives/<filename>/`. Note this overwrites that directory's
    manifest with one describing only the waveform — run derivatives() afterward to
    restore a manifest covering the full set if that matters.

    :param str ark: Item ARK identifier.
    :param dict opts: 'width' (int, default CONFIG["audio"]["waveform_width"]), 'height'
        (int, default CONFIG["audio"]["waveform_height"]), 'color' (str, default
        CONFIG["audio"]["waveform_color"]), 'app' (str) — target gold app, see transcode().
    :returns: {"status": "success", "ark": str, "audios": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}}
    :rtype: dict
    """
    app = opts.get("app")
    item = items.resolve(ark)
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
                command=["digtk", "audio", "waveform", ark],
                params={k: v for k, v in opts.items() if k in ("width", "height", "color")},
                type="gold", type_opts={"app": app},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )

    return {"status": "success", "ark": ark, "audios": audios}


def derivatives(ark, opts={}):
    """Generate the full derivative set for every source audio file in the item —
    the umbrella `digtk public-dc audio derivatives` calls: transcode + waveform together into
    one directory per source, then transcribes every source to a WebVTT caption via
    captions.transcribe_sources() (local Whisper ASR — machine-generated and
    unreviewed, see docs/cask-conventions.md's Captions/Transcripts section) using the
    same staged sources, merging each caption into the AudioObject entry
    update_associated_media() just wrote. Captions still publish into their own flat,
    shared 'captions/' directory, unaffected by this per-source layout.

    :param str ark: Item ARK identifier.
    :param dict opts: 'format' (str, default CONFIG["audio"]["transcode_format"]),
        'width'/'height' (waveform dimensions); 'language'/'model' for the caption
        step (see captions.transcribe_sources()); forwarded to ffmpeg_utils. 'app'
        (str) — target gold app for the transcode/waveform + associated-media
        sidecar + captions.
    :returns: {"status": "success", "ark": str, "derivatives": {filename: {"dir": str,
        "outputs": [...], "manifest": {...}}}, "captions": {...}}
    :rtype: dict
    """
    fmt = opts.get("format", CONFIG["audio"]["transcode_format"])
    app = opts.get("app")
    item = items.resolve(ark)
    audio_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "audio")
    start = time.monotonic()

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": audio_paths})
        total = len(sources)
        derivatives_results = {}
        audio_objects = []

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
                command=["digtk", "audio", "derivatives", ark],
                params={"format": fmt},
                type="gold", type_opts={"app": app},
                opts={
                    "manifest_opts": {"duration_s": time.monotonic() - start},
                    "link_relation": _LINK_RELATION,
                },
            )

            audio_objects.append({
                "@type": "AudioObject",
                "identifier": stem,
                "contentUrl": pipeline.cask_public_ref(f"{derivatives_results[filename]['dir']}/{converted_filename}"),
                "encodingFormat": f"audio/{fmt}",
            })
            utils.log(f"[{i}/{total}] {stem}: done")

        utils.log("Updating associated-media sidecar...")
        pipeline.update_associated_media(item, audio_objects, [src["bronze_path"] for src in sources], "audio", app=app)

        utils.log("Transcribing captions...")
        captions_result = captions.transcribe_sources(
            item, sources, tmp_dir,
            command=["digtk", "audio", "derivatives", ark],
            opts=opts,
        )

    return {"status": "success", "ark": ark, "derivatives": derivatives_results, "captions": captions_result}
