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

from digtk.lib import fs, utils, whisper_utils
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


def transcribe_sources(item, sources, tmp_dir, command, opts={}):
    """Transcribe an already-staged set of source video/audio files to WebVTT
    caption files (see attach_captions() for how they reach the search index).

    Called directly by video.derivatives()/audio.derivatives() using the sources
    they've already staged into their own tmp_dir — never re-stages, so captions
    generate as part of that same derivative run rather than a separate pass.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param list[dict] sources: Already-staged sources (see pipeline.stage_sources()) —
        every source video.derivatives()/audio.derivatives() is generating other
        derivatives for in this same run.
    :param str tmp_dir: Local directory to write caption files into (the caller's own
        tmp_dir — shared, not a new one).
    :param list[str] command: Full argv of the umbrella command driving this call
        (e.g. ["digtk", "video", "derivatives", ark, ...]), recorded in the captions
        manifest/lineage the same way every other publish() call in that run is.
    :param dict opts: 'language' (str, default CONFIG["captions"]["language"]), 'model'
        (str, default CONFIG["captions"]["model"], must be one of
        digtk.lib.whisper_utils.MODEL_CHOICES).
    :returns: {"dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    language = opts.get("language", CONFIG["captions"]["language"])
    model = opts.get("model", CONFIG["captions"]["model"])
    start = time.monotonic()
    total = len(sources)
    multi = total > 1
    local_outputs = []

    for i, src in enumerate(sources, start=1):
        stem = src["stem"]
        utils.log(f"[{i}/{total}] {stem}: transcribing (model={model})...")
        filename = pipeline.output_name(stem, f"output-{language}.vtt", multi)
        local_dest = os.path.join(tmp_dir, filename)
        whisper_utils.transcribe(src["local_path"], local_dest, opts=opts)
        local_outputs.append((filename, local_dest, src["bronze_path"]))

    result = pipeline.publish(
        item, "captions", local_outputs,
        command=command,
        params={"language": language, "model": model},
        opts={
            "manifest_opts": {"duration_s": time.monotonic() - start},
            "link_relation": _LINK_RELATION,
        },
    )

    return result
