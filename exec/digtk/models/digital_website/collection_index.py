"""
Collection-wide showcase ES writes (bulk reindex, orphan prune, delete) and
removal of a deleted bronze item's silver and gold outputs.
Builds on digtk.lib.showcase_index.
"""
import json

from digtk.lib import fs, items, showcase_index, utils
from digtk.models import collection as collection_model
from digtk.models.digital_website import collection_record, item_record

DEFAULT_BATCH_SIZE = 200

# Most of the indexed items a prune may delete.
DEFAULT_MAX_PRUNE_FRACTION = 0.5


def _project_opts(opts):
    """Just the 'project' option, for gold reads and writes.

    :param dict opts: Caller options.
    :rtype: dict
    """
    return {"project": opts["project"]} if opts.get("project") else {}


def _chunk_opts(opts):
    """Just the 'chunk_size' option, for bulk writes.

    :param dict opts: Caller options.
    :rtype: dict
    """
    return {"chunk_size": opts["chunk_size"]} if opts.get("chunk_size") else {}


def collection_es_id(key, opts={}):
    """A collection's ES document id, from its bronze ARK or, once bronze is
    gone, its gold record.

    :param str key: Collection key, [bronze/]<group>/<collection>.
    :param dict opts: 'project' — see collection_record.read().
    :returns: e.g. 'https://digital.ucdavis.edu/collection/ark:/87287/d7root'.
    :rtype: str
    :raises LookupError: If neither bronze nor gold gives the collection an ARK.
    """
    group, name = collection_record.parse_key(key)
    try:
        return f"{item_record.COLLECTION_URI_PREFIX}{collection_model.get_ark(name, group)}"
    except LookupError as e:
        try:
            root = collection_record.root_node(collection_record.read(name, _project_opts(opts)))
        except FileNotFoundError:
            raise LookupError(f"{key}: no collection ark in bronze or gold — {e}") from e
        if not root.get("@id"):
            raise LookupError(f"{key}: gold collection record has no root node") from e
        return root["@id"]


def _gold_ark(folder, opts):
    """The ARK in a bronze folder's gold record, or None without one.

    :param str folder: Bronze item folder.
    :param dict opts: 'project'.
    :rtype: str|None
    """
    try:
        return item_record.read(items.location_for_folder(folder), opts).get("@id")
    except FileNotFoundError:
        return None


def gold_item_folders(key, opts={}):
    """The bronze folders of every item with a gold record in a collection,
    from each record's manifest.json. Reads gold only.

    :param str key: Collection key, [bronze/]<group>/<collection>.
    :param dict opts: 'project' (see item_record._project()).
    :returns: Bronze item folders, sorted.
    :rtype: list[str]
    """
    _, name = collection_record.parse_key(key)
    folders = []

    def walk(path):
        try:
            entries = fs.ls(path)
        except FileNotFoundError:
            return
        if any(e.kind == "file" and e.name == items.ITEM_RECORD_FILENAME for e in entries):
            manifest = f"{path}/manifest.json"
            bronze = json.loads(fs.read(manifest)).get("bronze") if fs.exists(manifest) else None
            if bronze:
                folders.append(bronze)
            else:
                utils.warn(f"{path}: gold record has no manifest bronze folder, skipped")
            return
        for entry in entries:
            if entry.kind == "directory":
                walk(f"cask:{entry.path}")

    walk(f"cask:/gold/{item_record._project(opts)}/{name}")
    return sorted(folders)


def reindex_items(folders, opts={}):
    """Bulk-indexes items given by bronze folder, from their gold records.
    Without 'rebuild' bronze is never read and a folder with no gold record
    is skipped; with it, build-item rebuilds gold from bronze first.

    :param list[str] folders: Bronze item folders.
    :param dict opts: 'rebuild' (bool — run item_record.build() first),
        'project', 'chunk_size' (see showcase_index.bulk()).
    :returns: {"indexed": [{"folder", "ark"}], "failed": [{"folder", "ark", "error"}],
        "skipped": [folder], "pages": int}.
    :rtype: dict
    """
    project_opts = _project_opts(opts)
    result = {"indexed": [], "failed": [], "skipped": [], "pages": 0}
    folder_for = {}
    for folder in folders:
        if not opts.get("rebuild"):
            ark = _gold_ark(folder, project_opts)
            if not ark:
                result["skipped"].append(folder)
                continue
            folder_for[ark] = folder
            continue
        try:
            ark = items.ark_for_path(folder)
        except LookupError:
            result["skipped"].append(folder)
            continue
        try:
            item_record.build(folder, project_opts)
        except Exception as e:
            result["failed"].append({"folder": folder, "ark": ark, "error": f"build-item: {e}"})
            continue
        folder_for[ark] = folder

    indexed = showcase_index.index_items(list(folder_for), {**project_opts, **_chunk_opts(opts)})
    result["indexed"] = [{"folder": folder_for[ark], "ark": ark} for ark in indexed["indexed"]]
    result["failed"] += [{"folder": folder_for[f["ark"]], **f} for f in indexed["failed"]]
    result["pages"] = indexed["pages"]
    return result


def prune_collection(key, opts={}):
    """Deletes indexed items that are no longer in the collection's bronze
    items root. Refuses a prune that would delete too much unless forced.

    :param str key: Collection key, [bronze/]<group>/<collection>.
    :param dict opts: 'force' (bool), 'dry_run' (bool), 'max_fraction'
        (float, default 0.5), 'project'.
    :returns: {"collection", "indexed": int, "current": int, "orphans": [id],
        "items": int, "pages": int, "errors": [...], "dryRun": bool}.
    :rtype: dict
    :raises RuntimeError: If the guard refuses the prune.
    """
    group, name = collection_record.parse_key(key)
    collection_id = collection_es_id(key, opts)
    current = {
        showcase_index.item_es_id(ark)
        for ark in collection_model.list_item_arks(name, {"group": group})
    }
    indexed = showcase_index.list_collection_item_ids(collection_id)
    orphans = sorted(set(indexed) - current)

    max_fraction = opts.get("max_fraction", DEFAULT_MAX_PRUNE_FRACTION)
    if orphans and not opts.get("force"):
        if not current:
            raise RuntimeError(
                f"{key}: bronze lists no items but {len(indexed)} are indexed — "
                "refusing to prune (use --force)"
            )
        if len(orphans) > max_fraction * len(indexed):
            raise RuntimeError(
                f"{key}: prune would delete {len(orphans)} of {len(indexed)} indexed items, "
                f"more than {max_fraction:.0%} — refusing (use --force)"
            )

    result = {
        "collection": key, "indexed": len(indexed), "current": len(current), "orphans": orphans,
        "items": 0, "pages": 0, "errors": [], "dryRun": bool(opts.get("dry_run")),
    }
    if orphans and not opts.get("dry_run"):
        result.update(showcase_index.delete_items(orphans, _chunk_opts(opts)))
    return result


def reindex_collection(key, opts={}):
    """Reindexes every gold item of a collection (every bronze item with
    'rebuild') in bulk batches, prunes orphans, then refreshes the collection
    document. The prune is skipped if any item failed.

    :param str key: Collection key, [bronze/]<group>/<collection>.
    :param dict opts: 'rebuild', 'prune' (bool, default True), 'force_prune',
        'batch_size' (int, default 200), 'project', 'chunk_size'.
    :returns: {"collection", "items": int, "indexed": int, "failed": [...],
        "skipped": [...], "pages": int, "prune": dict, "collectionDoc": dict}.
    :rtype: dict
    """
    group, name = collection_record.parse_key(key)
    if opts.get("rebuild"):
        folders = collection_model.list_item_folders(name, {"group": group})
    else:
        folders = gold_item_folders(key, _project_opts(opts))
    batch_size = opts.get("batch_size") or DEFAULT_BATCH_SIZE

    summary = {"collection": key, "items": len(folders), "indexed": 0, "failed": [], "skipped": [], "pages": 0}
    for start in range(0, len(folders), batch_size):
        batch = reindex_items(folders[start:start + batch_size], opts)
        summary["indexed"] += len(batch["indexed"])
        summary["failed"] += batch["failed"]
        summary["skipped"] += batch["skipped"]
        summary["pages"] += batch["pages"]
        utils.log(f"{key}: indexed {summary['indexed']} of {len(folders)} item(s)")

    if opts.get("prune") is False:
        summary["prune"] = {"skipped": "disabled"}
    elif summary["failed"]:
        summary["prune"] = {"skipped": f"{len(summary['failed'])} item(s) failed"}
    else:
        summary["prune"] = prune_collection(key, {**opts, "force": opts.get("force_prune")})

    summary["collectionDoc"] = showcase_index.index_collection(name, _project_opts(opts))
    return summary


def delete_collection(key, opts={}):
    """Removes a collection, its items and their pages from the showcase
    indexes. Bronze, silver, gold and Postgres are left alone.

    :param str key: Collection key, [bronze/]<group>/<collection>.
    :param dict opts: 'project' — see collection_es_id().
    :returns: showcase_index.delete_collection()'s counts, with "deleted"
        (bool, the collection doc), "collection" (the key) and "esId".
    :rtype: dict
    :raises LookupError: If the collection's ES id can't be worked out.
    """
    collection_id = collection_es_id(key, opts)
    result = showcase_index.delete_collection(collection_id, _chunk_opts(opts))
    return {**result, "deleted": result["collection"], "collection": key, "esId": collection_id}


def delete_items(arks, opts={}):
    """Removes items and their pages from the showcase indexes. Also reports
    each item's collection key, read from gold, when it can still be found.

    :param list[str] arks: Item ARKs.
    :param dict opts: 'project' (see item_record.gold_location()).
    :returns: {"items": int, "pages": int, "errors": [...],
        "deleted": [{"ark", "esId", "collection": key|None}], "collections": [key]}.
    :rtype: dict
    """
    deleted = []
    for ark in arks:
        # bronze may already be gone
        try:
            location, _ = item_record.gold_location(ark, _project_opts(opts))
            collection_key = f"bronze/{location.group}/{location.collection}"
        except (LookupError, FileNotFoundError):
            collection_key = None
        deleted.append({"ark": ark, "esId": showcase_index.item_es_id(ark), "collection": collection_key})
    result = showcase_index.delete_items([d["esId"] for d in deleted])
    collections = sorted({d["collection"] for d in deleted if d["collection"]})
    return {**result, "deleted": deleted, "collections": collections}


def _count_files(path):
    """Files under a CaskFS directory, recursively.

    :param str path: fs-compatible directory path.
    :rtype: int
    """
    count = 0
    for entry in fs.ls(path):
        if entry.kind == "directory":
            count += _count_files(f"cask:{entry.path}")
        else:
            count += 1
    return count


def item_output_dirs(folder):
    """The silver directory and every gold project's directory for a bronze
    item folder.

    :param str folder: Bronze item folder, e.g. '/bronze/g/c/latest/tree/foo'.
    :returns: CaskFS paths, e.g. ['cask:/silver/g/c/latest/tree/foo', 'cask:/gold/digital-dev/c/tree/foo'].
    :rtype: list[str]
    :raises ValueError: If the folder has no item path below its time segment.
    """
    group, collection, time, item_path = items._parse_bronze_folder(folder)
    if not item_path:
        raise ValueError(f"{folder}: not an item folder")
    dirs = [f"cask:/silver/{group}/{collection}/{time}/{item_path}"]
    try:
        projects = [e.name for e in fs.ls("cask:/gold") if e.kind == "directory"]
    except FileNotFoundError:
        projects = []
    dirs += [f"cask:/gold/{project}/{collection}/{item_path}" for project in sorted(projects)]
    return dirs


def remove_item_outputs(folders, opts={}):
    """Deletes the silver and gold directories of items gone from bronze,
    logging each one. A folder that exists in bronze again is skipped.

    :param list[str] folders: Bronze item folders.
    :param dict opts: 'dry_run' (bool — report only).
    :returns: {"removed": [{"folder", "path", "files"}], "kept": [folder], "dryRun": bool}.
    :rtype: dict
    """
    result = {"removed": [], "kept": [], "dryRun": bool(opts.get("dry_run"))}
    for folder in folders:
        bronze = f"cask:/{folder.lstrip('/')}"
        if fs.exists(bronze):
            utils.log(f"{folder}: still in bronze, kept")
            result["kept"].append(folder)
            continue
        for path in item_output_dirs(folder):
            if not fs.exists(path):
                continue
            files = _count_files(path)
            verb = "would remove" if opts.get("dry_run") else "removed"
            if not opts.get("dry_run"):
                fs.delete(path, opts={"directory": True})
            utils.log(f"{folder}: {verb} {path} ({files} file(s))")
            result["removed"].append({"folder": folder, "path": path, "files": files})
    return result
