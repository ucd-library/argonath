"""
Dagster sensor definitions — chain pipeline phases on job completion, plus two
source-polling sensors for the collection registry (see
digtk.models.collection.register_collection/find_changed_preservica_collections
and exec/digtk/sql/collection_registry.sql, applied via `digtk collection init-pg`):

  bronze_preservica_auto_sync      — polls Preservica's /entities/updated-since
                                      and reconciles only the registered
                                      collections that actually changed.
  bronze_collection_partition_sync — keeps Dagster's 'collections' dynamic
                                      partition in sync with the registry, so a
                                      collection registered via CLI (before its
                                      first successful reconcile run) is
                                      immediately visible as a partition.

Plus bronze_collection_cask_audit, which reads CaskFS's audit log for bronze item
changes (see digtk.models.cask_changes), and workspace_partition_sync, which keeps the 'workspace' dynamic
partition in sync with the collections staged under CaskFS's /workspace (see
digtk.models.workspace) for lib.pipeline_workspace.workspace_process.
"""
import datetime
import json

import dagster as dg

from lib import collection_state
from lib.configs import CaskChangesConfig, collections_partitions
from lib.pipeline_index import gold_dcs_apply_cask_changes
from lib.utils import default_run_config, exec


# Default lookback for a sensor's very first tick, before it has a cursor of
# its own to resume from.
_INITIAL_LOOKBACK = datetime.timedelta(days=1)


@dg.sensor(
    job_name="bronze_reconcile_collection", 
    minimum_interval_seconds=300, 
    description=(
        "Listens for changes in Preservica for registered collections and syncs changes to the collection's bronze item list, rebuilding the tree structure. "
        "This should trigger the `bronze_collection_cask_audit` sensor to process any changed items and update the showcase indexes."
    ),
)
def bronze_preservica_auto_sync(context: dg.SensorEvaluationContext):
    """Poll Preservica for changes since the last tick and sync only the
    specific entities that changed, for only the registered collections
    actually affected — untouched collections are skipped entirely, and an
    affected collection gets a targeted sync of just its changed folder(s)/
    item(s), never a full re-walk (see
    digtk.models.collection.find_changed_preservica_collections(), which also
    auto-registers a brand-new labeled folder the first time it's seen, and
    preservica_sync_entities(), which reconcile_collection_items calls with
    each RunRequest's preservica_changed list).

    A registered Preservica collection is always checked here regardless of
    its workflow tags or whether it was first registered manually (`digtk
    preservica import`) or by this sensor's own auto-discovery.

    :param context: Dagster sensor evaluation context.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    since = context.cursor or (now - _INITIAL_LOOKBACK).isoformat()

    result = exec(["digtk", "preservica", "changes", "--since", since])
    changed = result.get("collections", [])

    context.update_cursor(now.isoformat())

    if not changed:
        return dg.SkipReason(f"No Preservica changes since {since}")

    return [
        dg.RunRequest(
            run_key=f"preservica-sync-{c['group']}-{c['name']}-{now.isoformat()}",
            run_config={"ops": {
                "reconcile_collection_items": {"config": {
                    "group": c["group"],
                    "collection": c["name"],
                    "source_type": "preservica",
                    "preservica_changed": [{"ref": e["ref"], "type": e["type"]} for e in c["changed"]],
                }},
                "rebuild_collection_tree": {"config": {
                    "group": c["group"],
                    "collection": c["name"],
                    "source_type": "preservica",
                    "preservica_changed": [{"ref": e["ref"], "type": e["type"]} for e in c["changed"]],
                }},
            }},
        )
        for c in changed
    ]


@dg.sensor(
    minimum_interval_seconds=300, 
    description=(
        "Listens for collections that have been added to the registry (Postgres) and adds a Dagster partition for each new collection. "
        "Collections are added to the registry during `digtk collection add` or the `bronze_preservica_auto_sync` sensor."
    ),
)
def bronze_collection_partition_sync(context: dg.SensorEvaluationContext):
    """Add a Dagster 'collections' dynamic partition for every active
    registered collection that doesn't already have one — the bridge between
    `digtk collection add`/`digtk preservica import` (which always register,
    see digtk.models.collection.register_collection()) and Dagster, since a
    partition is otherwise only created lazily as a side effect of a
    *successful* reconcile_collection_items run.

    Add-only: a disabled collection's partition is deliberately left in place
    (pruning it is a separate, explicit ops action) so run/partition history is
    never silently severed.

    :param context: Dagster sensor evaluation context.
    """
    result = exec(["digtk", "collection", "list"])
    active = result.get("collections", [])

    existing = set(context.instance.get_dynamic_partitions("collections"))
    keys = [f"bronze/{c['group_name']}/{c['name']}" for c in active]
    missing = [key for key in keys if key not in existing]
    if missing:
        context.instance.add_dynamic_partitions("collections", missing)

    return dg.SkipReason(
        f"{len(missing)} partition(s) added, {len(active)} active collection(s) total"
    )


@dg.sensor(
    minimum_interval_seconds=120, 
    description=(
        "Listens for workspace folders staged under CaskFS's /workspace/<casId> and adds a Dagster partition for each new folder. "
        "Workspace folders are added via `digtk workspace add`."
    ),
)
def workspace_partition_sync(context: dg.SensorEvaluationContext):
    """Keep the 'workspace' dynamic partition in sync with every workspace
    folder (anything under /workspace/[username]/[folder] with a
    source/ directory — see `digtk workspace ls`), so a folder staged with
    `digtk workspace add` shows up as a pickable partition in the launchpad.

    Unlike bronze_collection_partition_sync, this also removes partitions whose
    workspace folder is gone: workspace is scratch space, so a deleted
    folder has no run history worth keeping a partition around for.

    :param context: Dagster sensor evaluation context.
    """
    result = exec(["digtk", "workspace", "ls"])
    current = {f["key"] for f in result.get("folders", [])}

    existing = set(context.instance.get_dynamic_partitions("workspace"))
    missing = sorted(current - existing)
    stale = sorted(existing - current)
    if missing:
        context.instance.add_dynamic_partitions("workspace", missing)
    for key in stale:
        context.instance.delete_dynamic_partition("workspace", key)

    return dg.SkipReason(
        f"{len(missing)} partition(s) added, {len(stale)} removed, {len(current)} workspace folder(s) total"
    )


# Longest folder list shown in one tag.
_TAG_MAX_LENGTH = 200


def _names_tag(folders):
    """Item folder names for a run tag, shortened with a '+N more' tail.

    :param list[str] folders: Bronze item folders.
    :returns: e.g. "audio-example, quarter-mile +3 more", or "" for none.
    :rtype: str
    """
    names = [f.rstrip("/").rsplit("/", 1)[-1] for f in folders]
    shown = []
    for i, name in enumerate(names):
        tail = f" +{len(names) - i} more"
        if shown and len(", ".join(shown + [name])) + len(tail) > _TAG_MAX_LENGTH:
            return ", ".join(shown) + tail
        shown.append(name)
    return ", ".join(shown)


def _change_tags(reindex, cleanup):
    """Run tags naming the items a run changes, one tag per action.

    :param list[str] reindex: Folders to reindex.
    :param list[str] cleanup: Deleted folders.
    :rtype: dict
    """
    tags = {"argonath/changes": f"{len(reindex)} reindexed, {len(cleanup)} deleted"}
    for name, folders in (("reindexed", reindex), ("deleted", cleanup)):
        if folders:
            tags[f"argonath/{name}"] = _names_tag(folders)
    return tags


@dg.sensor(
    job=gold_dcs_apply_cask_changes,
    minimum_interval_seconds=300,
    description=(
        "Listens for changes to the CaskFS audit log and builds silver level derivatives and gold level "
        "metadata/binaries for each changed item, as well as the collection, and inserts into "
        "elastic for the showcase app (defaulting to private). To process a whole collection manually, run "
        "`gold_dcs_process_collection`."
    ),
)
def bronze_collection_cask_audit(context: dg.SensorEvaluationContext):
    """Act on settled bronze item changes from the CaskFS audit log.
    Items to process are queued for item_queue's gold_dcs_process_item runs.
    Other changes get one gold_dcs_apply_cask_changes run per collection.

    :param context: Dagster sensor evaluation context.
    """
    cursor = json.loads(context.cursor) if context.cursor else None
    result = collection_state.cask_changes(cursor)
    if cursor is None:
        context.update_cursor(json.dumps(result["cursor"]))
        return dg.SkipReason(f"Starting at audit event {result['cursor']['after']}")

    for skipped in result["skipped"]:
        context.log.info(f"[{skipped['folder']}] skipped: {skipped['reason']}")

    # Before the cursor moves, so failures retry.
    queued = 0
    if result["process"]:
        queued = collection_state.enqueue_items(result["process"], {"trigger": "cask-audit"})["enqueued"]
        context.log.info(f"{queued} item(s) queued for gold_dcs_process_item")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    requests = []
    keys = set(result["reindex"]) | set(result["delete"]) | set(result["cleanup"]) | set(result["collections"])
    for key in sorted(keys):
        if not context.instance.has_dynamic_partition(collections_partitions.name, key):
            context.instance.add_dynamic_partitions(collections_partitions.name, [key])
        run_config = default_run_config({"plan_cask_changes": CaskChangesConfig})
        changes = {
            "reindex": result["reindex"].get(key, []),
            "delete": result["delete"].get(key, []),
            "cleanup": result["cleanup"].get(key, []),
        }
        run_config["ops"]["plan_cask_changes"]["config"].update(changes)
        tags = {
            "argonath/collection": key, "argonath/trigger": "cask-audit",
            **_change_tags(changes["reindex"], changes["cleanup"]),
        }
        requests.append(dg.RunRequest(
            run_key=f"cask-changes-{key}-{now}", partition_key=key, run_config=run_config, tags=tags,
        ))

    context.update_cursor(json.dumps(result["cursor"]))
    if not requests:
        return dg.SkipReason(f"{queued} item(s) queued, {result['deferred']} change(s) still settling")
    return requests
