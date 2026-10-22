"""
Turns CaskFS audit-log events under registered collections' bronze items
into per-item work: process (derivatives and all), reindex (metadata only)
or delete from the showcase index. Read by Dagster's bronze_cask_audit_sensor.
"""
from datetime import datetime, timezone

from digtk.lib import cask_audit, fs, items
from digtk.models import collection as collection_model
from digtk.models.digital_website import item_record

DEFAULT_SETTLE_SECONDS = 300

# Files whose change needs no new derivatives.
METADATA_FILES = {"metadata.json", "identifiers.json"}
SIDECAR_SUFFIX = ".jsonld.json"

# Collection-level files collection_record.build() reads.
COLLECTION_FILES = ("tree/metadata.json", "tree/identifiers.json")


def _cask_path(path):
    """A path without its 'cask:' prefix or a trailing slash.

    :param str path: fs-compatible path.
    :rtype: str
    """
    return (path[len("cask:"):] if path.startswith("cask:") else path).rstrip("/")


def watched_roots(opts={}):
    """Every active registered collection's items root and collection files.

    :param dict opts: 'conn' (registry connection).
    :returns: [{"key", "group", "name", "items_root", "collection_files"}].
    :rtype: list[dict]
    """
    roots = []
    for row in collection_model.list_active_collections(opts=opts):
        group, name = row["group_name"], row["name"]
        latest = f"/bronze/{group}/{name}/latest"
        roots.append({
            "key": f"bronze/{group}/{name}", "group": group, "name": name,
            "items_root": _cask_path(collection_model.items_root(name, group)),
            "collection_files": [f"{latest}/{rel}" for rel in COLLECTION_FILES],
        })
    return roots


def target_for(path, resource_type, roots):
    """What an event path touches: an item, a collection's own files, or nothing.

    :param str path: CaskFS-absolute path from the audit row.
    :param str resource_type: 'file' or 'directory'.
    :param list[dict] roots: watched_roots()'s result.
    :returns: {"kind": "item"|"collection", "key", "folder"?, "rel"?}, or None.
    :rtype: dict|None
    """
    for root in roots:
        if path in root["collection_files"]:
            return {"kind": "collection", "key": root["key"]}
        prefix = root["items_root"] + "/"
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        segment, _, rel = rest.partition("/")
        # files directly under the items root
        if not rel and resource_type == "file":
            return None
        return {"kind": "item", "key": root["key"], "folder": prefix + segment, "rel": rel}
    return None


def _group_events(events, roots):
    """Groups events by the item or collection they touch.

    :param list[dict] events: cask_audit.read_events() rows.
    :param list[dict] roots: watched_roots()'s result.
    :returns: {group id: {"kind", "key", "folder"?, "files": set, "first_id", "last_id", "last_created"}}.
    :rtype: dict
    """
    groups = {}
    for event in events:
        paths = [event["resource_path"]]
        from_path = (event.get("details") or {}).get("fromPath")
        if from_path:
            paths.append(from_path)
        for path in paths:
            target = target_for(path, event["resource_type"], roots)
            if not target:
                continue
            gid = target["folder"] if target["kind"] == "item" else f"collection:{target['key']}"
            group = groups.setdefault(gid, {
                **{k: v for k, v in target.items() if k != "rel"},
                "files": set(), "dirs": False, "first_id": event["audit_log_id"],
            })
            group["last_id"] = event["audit_log_id"]
            group["last_created"] = event["created"]
            if event["resource_type"] == "directory":
                group["dirs"] = True
            elif target.get("rel"):
                group["files"].add(target["rel"])
    return groups


def _metadata_only(group):
    """Whether every changed file in an item needs no new derivatives.

    :param dict group: A _group_events() group.
    :rtype: bool
    """
    if group["dirs"] or not group["files"]:
        return False
    names = [rel.rsplit("/", 1)[-1] for rel in group["files"]]
    return all(n in METADATA_FILES or n.endswith(SIDECAR_SUFFIX) for n in names)


def _classify(group, current_arks):
    """Decides process, reindex, delete or skip for one settled item.

    :param dict group: A _group_events() item group.
    :param callable current_arks: key -> set of ARKs now in that collection.
    :returns: ("process"|"reindex"|"delete"|"gone", ark|None, reason). "gone"
        is a deleted folder whose ES doc stays (never indexed, or moved).
    :rtype: tuple
    """
    folder = group["folder"]
    location = items.location_for_folder(folder)
    gold_path = item_record.record_path(location)
    if fs.exists(f"cask:{folder}"):
        if _metadata_only(group) and fs.exists(gold_path):
            return "reindex", None, "metadata changed"
        return "process", None, "new or changed binaries"
    try:
        ark = item_record.read(location).get("@id")
    except FileNotFoundError:
        return "gone", None, "deleted, never indexed"
    if not ark:
        return "gone", None, "deleted, gold record has no ark"
    # moved, not deleted
    if ark in current_arks(group["key"]):
        return "gone", ark, "ark still in the collection"
    return "delete", ark, "folder deleted"


def start_cursor(opts={}):
    """A cursor at the newest audit event, so history isn't replayed.

    :param dict opts: 'conn' (CaskFS connection).
    :rtype: dict
    """
    return {"after": cask_audit.max_event_id(opts), "done": {}}


def changes(cursor, opts={}):
    """Reads audit events after the cursor and returns settled per-item work.
    An item or collection is settled once its newest event is older than
    'settle_seconds'; unsettled ones are left for a later call.

    :param dict cursor: {"after": int, "done": {group id: last event id}}.
    :param dict opts: 'settle_seconds' (default 300), 'limit', 'now'
        (datetime, for tests), 'conn' (CaskFS), 'registry_conn'.
    :returns: {"process": [folder], "reindex": {key: [folder]}, "delete": {key: [ark]},
        "cleanup": {key: [folder]} (every deleted folder, for its silver/gold),
        "collections": [key], "skipped": [{"folder", "reason"}], "deferred": int, "cursor": dict}.
    :rtype: dict
    """
    after = int(cursor.get("after", 0))
    done = dict(cursor.get("done") or {})
    roots = watched_roots({"conn": opts["registry_conn"]} if opts.get("registry_conn") else {})
    prefixes = sorted({r["items_root"] + "/" for r in roots} | {p for r in roots for p in r["collection_files"]})
    read_opts = {"prefixes": prefixes, "limit": opts.get("limit")}
    if opts.get("conn"):
        read_opts["conn"] = opts["conn"]
    events = cask_audit.read_events(after, read_opts)

    now = opts.get("now") or datetime.now(timezone.utc)
    settle = opts.get("settle_seconds", DEFAULT_SETTLE_SECONDS)
    arks_by_key = {}

    def current_arks(key):
        if key not in arks_by_key:
            root = next(r for r in roots if r["key"] == key)
            arks_by_key[key] = set(collection_model.list_item_arks(root["name"], {"group": root["group"]}))
        return arks_by_key[key]

    result = {"process": [], "reindex": {}, "delete": {}, "cleanup": {}, "collections": [], "skipped": [], "deferred": 0}
    deferred_ids = []
    for gid, group in _group_events(events, roots).items():
        if done.get(gid, 0) >= group["last_id"]:
            continue
        if (now - group["last_created"]).total_seconds() < settle:
            deferred_ids.append(group["first_id"])
            continue
        done[gid] = group["last_id"]
        if group["kind"] == "collection":
            result["collections"].append(group["key"])
            continue
        action, ark, reason = _classify(group, current_arks)
        if action == "process":
            result["process"].append(group["folder"])
        elif action == "reindex":
            result["reindex"].setdefault(group["key"], []).append(group["folder"])
        else:
            if action == "delete":
                result["delete"].setdefault(group["key"], []).append(ark)
            else:
                result["skipped"].append({"folder": group["folder"], "reason": reason})
            result["cleanup"].setdefault(group["key"], []).append(group["folder"])

    if deferred_ids:
        new_after = min(deferred_ids) - 1
    else:
        new_after = max([after] + [e["audit_log_id"] for e in events])
    result["deferred"] = len(deferred_ids)
    result["cursor"] = {"after": new_after, "done": {g: i for g, i in done.items() if i > new_after}}
    return result
