"""
Caption/transcript generation for video and audio items.

Shared entry point for both media types — whisper's CLI works unmodified on either
(it pulls audio from a video file itself via ffmpeg internally), so there's no need for
a separate video-captions/audio-captions split.

Machine-generated and unreviewed: every caption's argonath:fieldSources entry is
flagged 'ai' (see docs/cask-conventions.md's Field Sources table), matching this
project's existing provenance convention, so downstream review workflows know it needs
a human pass — the same warning the reference dams-video-transcribe CLI prints, encoded
here as structured metadata instead of console text.

Structurally parallel to the other *_website.py models, built on
digtk.models.digital_website.pipeline for staging/publishing. Captions only publish
files here; attach_captions() adds them, as a 'caption' MediaObject, to whichever
VideoObject/AudioObject entry shares their source's 'identifier' (see
digtk.models.digital_website.video.media_objects()/audio.media_objects()) when the
item is indexed for the website (see digtk.lib.showcase_index._silver_media()).

There's no standalone `digtk captions transcribe` command — transcribe_sources() is
called directly by video.derivatives()/audio.derivatives() using the sources they've
already staged, so captions generate as part of the normal derivative run rather than
a separate pass.
"""
import json
import os
import time

from digtk.lib import ffmpeg_utils, fs, utils, whisper_utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS
from digtk.models.digital_website import pipeline

_LINK_RELATION = f"{ARGONATH_NS}captionOf"


def attach_captions(item, entries):
    """Add each published caption to the VideoObject/AudioObject entry its
    source belongs to, read back from the item's captions/manifest.json — the
    caption filename is matched to an entry's 'identifier' (its source stem)
    the same way transcribe_sources() named it.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[dict] entries: The item's "video" or "audio" associatedMedia
        entries (see video.media_objects()/audio.media_objects()); updated in place.
    :returns: True if any caption was attached.
    :rtype: bool
    """
    captions_dir = pipeline.derivative_dir(item, "captions")
    if not entries or not fs.exists(f"{captions_dir}/manifest.json"):
        return False
    manifest = json.loads(fs.read(f"{captions_dir}/manifest.json"))
    language = manifest.get("params", {}).get("language")
    outputs = set(manifest.get("outputs", []))
    multi = len(entries) > 1

    attached = False
    for entry in entries:
        filename = pipeline.output_name(entry["identifier"], f"output-{language}.vtt", multi)
        if filename not in outputs:
            continue
        entry["caption"] = {
            "@type": "MediaObject",
            "encodingFormat": "text/vtt",
            "inLanguage": language,
            "contentUrl": pipeline.cask_public_ref(f"{captions_dir}/{filename}"),
        }
        attached = True
    return attached


def caption_status(item, bronze_paths, opts={}):
    """Which sources already have a published caption, so a derivatives run
    can skip transcribing them (and staging them, if nothing else is missing).

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[str] bronze_paths: Every video/audio source in the run, in order.
    :param dict opts: 'language' (str, default CONFIG["captions"]["language"]),
        'force' (bool — report nothing as published).
    :returns: {bronze_path: published caption filename, or None}, in order.
    :rtype: dict
    """
    language = opts.get("language", CONFIG["captions"]["language"])
    multi = len(bronze_paths) > 1
    published = pipeline.existing_outputs(item, "captions", {"force": opts.get("force")})["files"]
    status = {}
    for path in bronze_paths:
        stem = os.path.splitext(os.path.basename(path))[0]
        filename = pipeline.output_name(stem, f"output-{language}.vtt", multi)
        status[path] = filename if filename in published else None
    return status


def whisper_timeout(path, opts={}):
    """Whisper timeout for one source, scaled to its duration so long
    recordings aren't cut off: the larger of captions.timeout_s and
    duration * captions.timeout_factor.

    :param str path: Local media file path.
    :param dict opts: 'timeout_s' / 'timeout_factor' override the CONFIG
        values; forwarded to ffmpeg_utils.probe().
    :returns: Timeout in seconds.
    :rtype: float
    """
    floor = opts.get("timeout_s", CONFIG["captions"]["timeout_s"])
    factor = opts.get("timeout_factor", CONFIG["captions"]["timeout_factor"])
    duration = ffmpeg_utils.probe(path, opts).get("duration_s")
    if not duration:
        return floor
    return max(floor, duration * factor)


def transcribe_sources(item, sources, tmp_dir, command, opts={}):
    """Transcribe an already-staged set of source video/audio files to WebVTT
    caption files (see attach_captions() for how they reach the search index).

    Called directly by video.derivatives()/audio.derivatives() using the sources
    they've already staged into their own tmp_dir — never re-stages, so captions
    generate as part of that same derivative run rather than a separate pass.
    Sources already captioned (opts['captioned']) are skipped and kept, and
    sources with no audio stream (e.g. silent film) are skipped with no caption.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[dict] sources: Already-staged sources (see pipeline.stage_sources()) —
        every source still needing a caption.
    :param str tmp_dir: Local directory to write caption files into (the caller's own
        tmp_dir — shared, not a new one).
    :param list[str] command: Full argv of the umbrella command driving this call
        (e.g. ["digtk", "video", "derivatives", ark, ...]), recorded in the captions
        manifest/lineage the same way every other publish() call in that run is.
    :param dict opts: 'language' (str, default CONFIG["captions"]["language"]), 'model'
        (str, default CONFIG["captions"]["model"], must be one of
        digtk.lib.whisper_utils.MODEL_CHOICES), 'captioned' (caption_status()'s
        return covering every source in the run; default: none captioned).
    :returns: {"dir": str, "outputs": [...], "manifest": {...}} — 'manifest' is
        None, and nothing is published, when no source produced or kept a caption.
    :rtype: dict
    """
    language = opts.get("language", CONFIG["captions"]["language"])
    model = opts.get("model", CONFIG["captions"]["model"])
    start = time.monotonic()
    captioned = opts.get("captioned") or {src["bronze_path"]: None for src in sources}
    staged = {src["bronze_path"]: src for src in sources}
    total = len(captioned)
    multi = total > 1
    local_outputs, kept_outputs = [], []

    for i, bronze_path in enumerate(captioned, start=1):
        stem = os.path.splitext(os.path.basename(bronze_path))[0]
        if captioned[bronze_path]:
            pipeline.log_skip(f"[{i}/{total}] {stem}", "caption")
            kept_outputs.append((captioned[bronze_path], bronze_path))
            continue
        src = staged[bronze_path]
        if not ffmpeg_utils.has_audio_stream(src["local_path"], opts):
            # Whisper silently writes nothing here
            utils.log(f"[{i}/{total}] {stem}: no audio stream, skipping caption")
            continue
        timeout = whisper_timeout(src["local_path"], opts)
        utils.log(f"[{i}/{total}] {stem}: transcribing (model={model}, timeout={timeout:.0f}s)...")
        filename = pipeline.output_name(stem, f"output-{language}.vtt", multi)
        local_dest = os.path.join(tmp_dir, filename)
        whisper_utils.transcribe(src["local_path"], local_dest, opts={**opts, "timeout": timeout})
        local_outputs.append((filename, local_dest, bronze_path))

    # Nothing to publish, e.g. all silent
    if not local_outputs and not kept_outputs:
        return {"dir": pipeline.derivative_dir(item, "captions"), "outputs": [], "manifest": None}

    result = pipeline.publish(
        item, "captions", local_outputs,
        command=command,
        params={"language": language, "model": model},
        opts={
            "manifest_opts": {"duration_s": time.monotonic() - start},
            "link_relation": _LINK_RELATION,
            "kept_outputs": kept_outputs,
        },
    )

    return result
