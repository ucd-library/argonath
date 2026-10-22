"""
Dagster sensor definitions — chain pipeline phases on job completion, plus two
source-polling sensors for the collection registry (see
digtk.models.collection.register_collection/find_changed_preservica_collections
and exec/digtk/sql/collection_registry.sql, applied via `digtk collection init-pg`):

  preservica_auto_sync_sensor      — polls Preservica's /entities/updated-since
                                      and reconciles only the registered
                                      collections that actually changed.
  collection_partition_sync_sensor — keeps Dagster's 'collections' dynamic
                                      partition in sync with the registry, so a
                                      collection registered via CLI (before its
                                      first successful reconcile run) is
                                      immediately visible as a partition.

Plus bronze_cask_audit_sensor, which reads CaskFS's audit log for bronze item
changes (see digtk.models.cask_changes), and workspace_partition_sync_sensor, which keeps the 'workspace' dynamic
partition in sync with the collections staged under CaskFS's /workspace (see
digtk.models.workspace) for lib.pipeline_workspace.process_workspace_job.
"""
import datetime
import json

import dagster as dg

from lib import collection_state
from lib.configs import CaskChangesConfig, collections_partitions
from lib.pipeline_index import apply_cask_changes_job
from lib.pipeline_item import normalize_item
from lib.utils import default_run_config, exec


# Default lookback for a sensor's very first tick, before it has a cursor of
# its own to resume from.
_INITIAL_LOOKBACK = datetime.timedelta(days=1)


@dg.sensor(job_name="reconcile_collection_job", minimum_interval_seconds=300)
def preservica_auto_sync_sensor(context: dg.SensorEvaluationContext):
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


@dg.sensor(minimum_interval_seconds=300)
def collection_partition_sync_sensor(context: dg.SensorEvaluationContext):
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


@dg.sensor(minimum_interval_seconds=120)
def workspace_partition_sync_sensor(context: dg.SensorEvaluationContext):
    """Keep the 'workspace' dynamic partition in sync with every workspace
    collection (anything under /workspace/[username]/[collection] with a
    source/ directory — see `digtk workspace ls`), so a folder staged with
    `digtk workspace add` shows up as a pickable partition in the launchpad.

    Unlike collection_partition_sync_sensor, this also removes partitions whose
    workspace collection is gone: workspace is scratch space, so a deleted
    folder has no run history worth keeping a partition around for.

    :param context: Dagster sensor evaluation context.
    """
    result = exec(["digtk", "workspace", "ls"])
    current = {c["key"] for c in result.get("collections", [])}

    existing = set(context.instance.get_dynamic_partitions("workspace"))
    missing = sorted(current - existing)
    stale = sorted(existing - current)
    if missing:
        context.instance.add_dynamic_partitions("workspace", missing)
    for key in stale:
        context.instance.delete_dynamic_partition("workspace", key)

    return dg.SkipReason(
        f"{len(missing)} partition(s) added, {len(stale)} removed, {len(current)} workspace collection(s) total"
    )


@dg.sensor(job_name="derivatives_job")
def derivatives_on_ingest(context: dg.SensorEvaluationContext):
    """Trigger derivatives_job when ingest_collection_job completes successfully.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("phase 0.4")


@dg.sensor(job_name="ocr_job")
def ocr_on_derivatives(context: dg.SensorEvaluationContext):
    """Trigger ocr_job when derivatives_job completes successfully.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("phase 0.5")


@dg.sensor(job_name="metadata_job")
def metadata_on_ocr(context: dg.SensorEvaluationContext):
    """Trigger metadata_job when ocr_job completes successfully.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("phase 0.6")


@dg.sensor(job_name="search_index_job")
def rag_on_metadata(context: dg.SensorEvaluationContext):
    """Trigger search_index_job when metadata_job completes successfully.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("phase 0.7")


@dg.sensor(job_name="showcase_search_index_job")
def showcase_search_on_derivatives(context: dg.SensorEvaluationContext):
    """Trigger showcase_search_index_job when derivatives_job completes successfully.

    Fires off derivatives_job, not metadata_job like rag_on_metadata does -
    see index_for_showcase_search's docstring in lib/assets.py for why the showcase
    search index deliberately doesn't wait on the AI metadata draft step.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("not yet wired - new asset, added alongside the fin/argonath migration's dams-router work, not part of the original phased buildout")


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


def _change_tags(process, reindex, cleanup):
    """Run tags naming the items a run changes, one tag per action.

    :param list[str] process: Folders to process.
    :param list[str] reindex: Folders to reindex.
    :param list[str] cleanup: Deleted folders.
    :rtype: dict
    """
    tags = {"argonath/changes": f"{len(process)} processed, {len(reindex)} reindexed, {len(cleanup)} deleted"}
    for name, folders in (("processed", process), ("reindexed", reindex), ("deleted", cleanup)):
        if folders:
            tags[f"argonath/{name}"] = _names_tag(folders)
    return tags


@dg.sensor(job=apply_cask_changes_job, minimum_interval_seconds=300)
def bronze_cask_audit_sensor(context: dg.SensorEvaluationContext):
    """Act on settled bronze item changes from the CaskFS audit log: one
    apply_cask_changes_job run per changed collection processes, reindexes,
    deletes (ES, silver and gold) and refreshes the collection doc.

    :param context: Dagster sensor evaluation context.
    """
    cursor = json.loads(context.cursor) if context.cursor else None
    result = collection_state.cask_changes(cursor)
    if cursor is None:
        context.update_cursor(json.dumps(result["cursor"]))
        return dg.SkipReason(f"Starting at audit event {result['cursor']['after']}")

    for skipped in result["skipped"]:
        context.log.info(f"[{skipped['folder']}] skipped: {skipped['reason']}")

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    requests = []
    process = {}
    for folder in result["process"]:
        process.setdefault(normalize_item(folder)[1], []).append(folder)
    keys = set(process) | set(result["reindex"]) | set(result["delete"]) | set(result["cleanup"]) | set(result["collections"])
    for key in sorted(keys):
        if not context.instance.has_dynamic_partition(collections_partitions.name, key):
            context.instance.add_dynamic_partitions(collections_partitions.name, [key])
        run_config = default_run_config({"fan_out_cask_changes": CaskChangesConfig})
        changes = {
            "process": process.get(key, []),
            "reindex": result["reindex"].get(key, []),
            "delete": result["delete"].get(key, []),
            "cleanup": result["cleanup"].get(key, []),
        }
        run_config["ops"]["fan_out_cask_changes"]["config"].update(changes)
        tags = {
            "argonath/collection": key, "argonath/trigger": "cask-audit",
            **_change_tags(changes["process"], changes["reindex"], changes["cleanup"]),
        }
        requests.append(dg.RunRequest(
            run_key=f"cask-changes-{key}-{now}", partition_key=key, run_config=run_config, tags=tags,
        ))

    context.update_cursor(json.dumps(result["cursor"]))
    if not requests:
        return dg.SkipReason(f"no settled changes, {result['deferred']} still settling")
    return requests
