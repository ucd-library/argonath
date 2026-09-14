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
"""
import datetime

import dagster as dg

from lib.utils import exec


# Default lookback for a sensor's very first tick, before it has a cursor of
# its own to resume from.
_INITIAL_LOOKBACK = datetime.timedelta(days=1)


@dg.sensor(job_name="reconcile_collection_job", minimum_interval_seconds=300)
def preservica_auto_sync_sensor(context: dg.SensorEvaluationContext):
    """Poll Preservica for changes since the last tick and reconcile only the
    registered collections actually affected — untouched collections are
    skipped entirely rather than re-walked (see
    digtk.models.collection.find_changed_preservica_collections(), which also
    auto-registers a brand-new labeled folder the first time it's seen).

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
            run_config={"ops": {"reconcile_collection_items": {"config": {
                "group": c["group"],
                "collection": c["name"],
                "source_type": "preservica",
                "source_uri": c["root_ref"],
            }}}},
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
    missing = [
        f"{c['group_name']}/{c['name']}" for c in active
        if f"{c['group_name']}/{c['name']}" not in existing
    ]
    if missing:
        context.instance.add_dynamic_partitions("collections", missing)

    return dg.SkipReason(
        f"{len(missing)} partition(s) added, {len(active)} active collection(s) total"
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


@dg.sensor(job_name="dams_search_index_job")
def dams_search_on_derivatives(context: dg.SensorEvaluationContext):
    """Trigger dams_search_index_job when derivatives_job completes successfully.

    Fires off derivatives_job, not metadata_job like rag_on_metadata does -
    see index_for_dams_search's docstring in lib/assets.py for why the DAMS
    search index deliberately doesn't wait on the AI metadata draft step.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("not yet wired - new asset, added alongside the fin/argonath migration's dams-router work, not part of the original phased buildout")
