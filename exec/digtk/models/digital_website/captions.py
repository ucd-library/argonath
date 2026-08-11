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
digtk.models.digital_website.pipeline for staging/publishing — but its associated-media
update is additive rather than a full replace: it merges a 'caption' MediaObject into
whichever existing VideoObject/AudioObject associatedMedia entry shares its
'identifier' (set by digtk.models.digital_website.video.derivatives()/audio.derivatives())
instead of overwriting the whole associatedMedia array the way
digtk.models.digital_website.pipeline.update_associated_media() does — that full-replace
behavior is correct for the *_website.py umbrellas (they regenerate every entry each
run) but wrong here, since captions are typically generated as a follow-up step after
derivatives() has already written the item's associatedMedia.
"""
import json
import os
import tempfile
import time

from digtk.lib import derivative, fs, items, utils, whisper_utils
from digtk.lib.config import CONFIG
from digtk.lib.schema import ARGONATH_NS, SCHEMA_CONTEXT
from digtk.models.digital_website import pipeline

_LINK_RELATION = f"{ARGONATH_NS}captionOf"


def _merge_caption_into_sidecar(item, captions_by_stem, captions_dir, language, source_files):
    """Inject a 'caption' MediaObject into whichever associatedMedia entry shares
    each caption's source stem as 'identifier'. If no sidecar or matching entry exists
    yet (e.g. captions run before derivatives()), a bare placeholder entry is added
    instead, so the caption is never silently dropped.

    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param dict captions_by_stem: {stem: caption filename}.
    :param str captions_dir: CaskFS captions/ process directory ('cask:/...').
    :param str language: BCP-47-ish language code used for every caption this call wrote.
    :param list[str] source_files: CaskFS paths this sidecar update is derived from.
    :returns: The full sidecar dict that was written.
    :rtype: dict
    """
    rel_path = f"{item.item_path}/item.jsonld.json" if item.item_path else "item.jsonld.json"
    write_opts = {"group": item.group, "time": item.time, "replace": True}
    dest = derivative.dest_path(type="silver", collection=item.collection, rel_path=rel_path, opts=write_opts)
    sidecar_path = f"cask:{dest}"

    if fs.exists(sidecar_path):
        doc = json.loads(fs.read(sidecar_path))
    else:
        doc = {"@context": SCHEMA_CONTEXT, "@id": item.ark, "ark": item.ark}
    doc.setdefault("associatedMedia", [])
    doc.setdefault("argonath:fieldSources", {})

    matched_stems = set()
    for entry in doc["associatedMedia"]:
        stem = entry.get("identifier")
        if stem in captions_by_stem:
            entry["caption"] = {
                "@type": "MediaObject",
                "encodingFormat": "text/vtt",
                "inLanguage": language,
                "contentUrl": pipeline.cask_public_ref(f"{captions_dir}/{captions_by_stem[stem]}"),
            }
            matched_stems.add(stem)

    for stem, filename in captions_by_stem.items():
        if stem not in matched_stems:
            doc["associatedMedia"].append({
                "@type": "MediaObject",
                "identifier": stem,
                "caption": {
                    "@type": "MediaObject",
                    "encodingFormat": "text/vtt",
                    "inLanguage": language,
                    "contentUrl": pipeline.cask_public_ref(f"{captions_dir}/{filename}"),
                },
            })

    doc["argonath:fieldSources"]["caption"] = "ai"
    derivative.write(
        type="silver", collection=item.collection, rel_path=rel_path,
        data=json.dumps(doc, indent=2).encode(), source_files=source_files, opts=write_opts,
    )
    return doc


def transcribe(ark, opts={}):
    """Transcribe every source video/audio file in the item to a WebVTT caption file.

    :param str ark: Item ARK identifier.
    :param dict opts: 'language' (str, default CONFIG["captions"]["language"]), 'model'
        (str, default CONFIG["captions"]["model"], must be one of
        digtk.lib.whisper_utils.MODEL_CHOICES).
    :returns: {"status": "success", "ark": str, "dir": str, "outputs": [...], "manifest": {...}}
    :rtype: dict
    """
    language = opts.get("language", CONFIG["captions"]["language"])
    model = opts.get("model", CONFIG["captions"]["model"])
    item = items.resolve(ark)
    media_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, {"video", "audio"})
    start = time.monotonic()
    multi = len(media_paths) > 1

    with tempfile.TemporaryDirectory() as tmp_dir:
        sources = pipeline.stage_sources(item, tmp_dir, opts={"bronze_binary_paths": media_paths})
        total = len(sources)
        local_outputs = []
        captions_by_stem = {}

        for i, src in enumerate(sources, start=1):
            stem = src["stem"]
            utils.log(f"[{i}/{total}] {stem}: transcribing (model={model})...")
            filename = pipeline.output_name(stem, f"output-{language}.vtt", multi)
            local_dest = os.path.join(tmp_dir, filename)
            whisper_utils.transcribe(src["local_path"], local_dest, opts=opts)
            local_outputs.append((filename, local_dest, src["bronze_path"]))
            captions_by_stem[stem] = filename

        result = pipeline.publish(
            item, "captions", local_outputs,
            command=["digtk", "captions", "transcribe", ark, "--language", language, "--model", model],
            params={"language": language, "model": model},
            opts={
                "manifest_opts": {"duration_s": time.monotonic() - start},
                "link_relation": _LINK_RELATION,
            },
        )

        utils.log("Merging captions into associated-media sidecar...")
        _merge_caption_into_sidecar(
            item, captions_by_stem, result["dir"], language, [src["bronze_path"] for src in sources]
        )

    return {"status": "success", "ark": ark, **result}
