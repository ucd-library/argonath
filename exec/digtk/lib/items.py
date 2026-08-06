"""
ARK -> CaskFS bronze location resolution.

Item paths in CaskFS are arbitrary (see docs/cask-conventions.md) — the ARK is
never derived from a path, only from a sidecar's `@id`/`ark` property. This
module is the single place that turns an ARK into a bronze location, via
CaskFS's Layer-3 `/cask/find` search (digtk.lib.fs.find_by_subject), so every
phase (derivatives, OCR, metadata) resolves items the same way.

Resolves to group/collection/time/item_path components (not a pre-built path
string) because that's what digtk.lib.derivative.write()'s
type/collection/rel_path/opts signature — the canonical way to write silver
outputs — needs to build the matching silver destination.

digtk.lib.fs.find_by_subject() (via CaskBackend.find_by_subject()) already
handles /cask/find's paginated {"totalCount", "results": [{"filepath": ...}]}
response shape and hands back a flat list of path strings — this module never
deals with pagination or the response envelope directly. CaskFS's API is
still under active development; if its shape changes again, that's the layer
to fix, not here.
"""
from dataclasses import dataclass

from digtk.lib import fs

BRONZE_PREFIX = "/bronze/"
SIDECAR_SUFFIX = ".jsonld.json"


@dataclass(frozen=True)
class ItemLocation:
    """An item's internal directory structure is arbitrary per source (see
    docs/cask-conventions.md's Bronze Layout) and can hold more than one binary —
    e.g. an archival box/folder containing several photographs. All binaries
    sharing an ARK are treated as one item.

    :param str ark: The item's ARK identifier.
    :param str group: Group namespace segment of the bronze path.
    :param str collection: Collection name segment.
    :param str time: Time-folder segment (e.g. 'latest', a date, a year-week).
    :param str item_path: Item's internal path relative to the collection/time root
        (e.g. 'box-1/folder-3'), possibly '' if binaries sit directly under time/.
    :param list[str] bronze_binary_paths: CaskFS paths ('cask:/...') to the item's one
        or more bronze source binaries, sorted.
    """
    ark: str
    group: str
    collection: str
    time: str
    item_path: str
    bronze_binary_paths: list


def _parse_bronze_path(path):
    """Split a bronze-absolute path into its group/collection/time/item_path/filename components.

    :param str path: e.g. '/bronze/digital-collections/my-coll/latest/box-1/folder-3/photo.tiff'.
    :returns: (group, collection, time, item_path, filename) — item_path is '' if the
        binary sits directly under the time/ folder.
    :rtype: tuple
    :raises ValueError: If path doesn't have at least group/collection/time/filename segments.
    """
    segments = path[len(BRONZE_PREFIX):].split("/")
    if len(segments) < 4:
        raise ValueError(f"Bronze path missing group/collection/time/filename segments: {path}")
    group, collection, time = segments[0], segments[1], segments[2]
    filename = segments[-1]
    item_path = "/".join(segments[3:-1])
    return group, collection, time, item_path, filename


def resolve(ark, opts={}):
    """Resolve an ARK to its bronze item location (one or more binaries).

    Finds every bronze sidecar file whose `@id` is the ARK — one for a
    single-binary item, more than one when the item is a multi-image box/folder —
    and derives each binary path by stripping the sidecar's '.jsonld.json' suffix.

    :param str ark: ARK identifier, e.g. 'ark:/87287/d7abc1234'.
    :param dict opts: Forwarded to fs.find_by_subject() (e.g. 'predicate' override).
    :returns: Resolved item location.
    :rtype: ItemLocation
    :raises LookupError: If no bronze sidecar matches the ARK, or matching binaries
        don't all share the same group/collection/time/item_path (a data-integrity
        problem — binaries sharing an ARK should live in the same item location).
    """
    matches = fs.find_by_subject(ark, opts)
    sidecar_paths = sorted(
        m for m in matches
        if m.startswith(BRONZE_PREFIX) and m.endswith(SIDECAR_SUFFIX)
    )

    if not sidecar_paths:
        raise LookupError(f"Expected exactly one bronze sidecar for {ark}, found 0")

    binary_paths = [p[: -len(SIDECAR_SUFFIX)] for p in sidecar_paths]
    parsed = [_parse_bronze_path(p) for p in binary_paths]
    locations = {(group, collection, time, item_path) for group, collection, time, item_path, _ in parsed}

    if len(locations) > 1:
        raise LookupError(
            f"Binaries for {ark} are not all in the same item location: {sorted(locations)}"
        )

    group, collection, time, item_path = locations.pop()

    return ItemLocation(
        ark=ark,
        group=group,
        collection=collection,
        time=time,
        item_path=item_path,
        bronze_binary_paths=[f"cask:{p}" for p in binary_paths],
    )
