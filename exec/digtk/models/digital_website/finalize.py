"""
Item-level finalize for showcase derivatives generated one file at a time.

`digtk showcase image|pdf derivatives --file ...` (one Dagster step per image
or PDF — see dagster/lib/pipeline_fanout.py) writes only what belongs to each
file and skips the outputs that combine every file of an item: an image item's
ocr/manifest.json, and the item sidecar's associatedMedia. finalize() writes
those once all of an item's files are done, by reading back what the per-file
runs published (see digtk.models.digital_website.image.finalize() and
.pdf.finalize()). Video and audio are still generated a whole item at a time,
so they have nothing to finalize.
"""
from digtk.lib import items
from digtk.models.digital_website import image, pdf, pipeline


def finalize(ark, opts={}):
    """Finalize every format of an item that has a finalize step (image, PDF).

    :param str ark: Item ARK identifier.
    :param dict opts: Forwarded to each format's finalize().
    :returns: {"status": "success", "ark": str, "image": {...}|absent, "pdf": {...}|absent}
        — one entry per format finalized.
    :rtype: dict
    :raises ValueError: If the item has neither image nor PDF binaries.
    :raises FileNotFoundError: If a file's per-file derivatives haven't been published.
    """
    formats = pipeline.detect_formats(items.resolve(ark).bronze_binary_paths)
    result = {"status": "success", "ark": ark}
    if "image" in formats:
        result["image"] = image.finalize(ark, opts)
    if "pdf" in formats:
        result["pdf"] = pdf.finalize(ark, opts)
    if len(result) == 2:
        raise ValueError(f"{ark} has no image or PDF binaries to finalize (formats: {sorted(formats)})")
    return result
