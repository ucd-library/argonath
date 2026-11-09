"""
Dagster asset definitions for the Argonath pipeline.

The bronze assets: importing a Preservica collection, reconciling a
collection against its source, and rebuilding its tree/ layout. Each calls the
digtk CLI via exec(). Per-item work (derivatives and the pike tools) runs as
lib.pipeline_item.gold_dcs_process_item, not as assets.
"""
import dagster as dg

from lib.configs import (
    PreservicaImportConfig,
    ReconcileCollectionConfig,
    RebuildTreeConfig,
)
from lib.utils import exec


@dg.asset(
    group_name="ingest",
)
def preservica_import_collection(context: dg.AssetExecutionContext, config: PreservicaImportConfig):
    """One-time full walk of a brand-new Preservica collection into bronze's
    by-ref/ layout (`digtk preservica import`), always registering the
    collection along the way, then a full rebuild of its tree/ layout from
    that by-ref/ (`digtk preservica tree`).

    Not partitioned, and config.collection is plain manual input: this
    collection has no Dagster partition yet, which is exactly why this asset
    exists separately from reconcile_collection_items — see that asset's own
    docstring ("A brand new collection's initial full import is out of scope
    here; that's still a manual `digtk preservica import`"). Once this run
    registers the collection, collection_partition_sync_sensor adds the
    'collections' dynamic partition on its next tick, and all later changes
    flow through reconcile_collection_items/preservica_auto_sync_sensor's
    targeted sync instead of another full walk.

    :param context: Dagster asset execution context.
    :param config: PreservicaImportConfig with ref, collection, group,
        max_depth, dest_root, source_dir, skip_unchanged.
    :returns: Dict with the resolved collection name, item/error/warning
        counts, and the tree rebuild's result under "tree".
    """
    cmd = [
        "digtk", "preservica", "import", config.ref,
        "--collection", config.collection,
        "--group", config.group,
        "--max-depth", str(config.max_depth),
    ]
    if config.dest_root:
        cmd += ["--dest-root", config.dest_root]
    if config.source_dir:
        cmd += ["--source-dir", config.source_dir]
    if config.skip_unchanged:
        cmd.append("--skip-unchanged")

    result = exec(cmd)
    collection = result.get("collection") or config.collection
    context.log.info(
        f"Imported {collection}: {result.get('items')} item(s), "
        f"{result.get('errors')} error(s), {result.get('warnings')} warning(s)"
    )

    # Full tree/ rebuild, same root as the import.
    tree_cmd = ["digtk", "preservica", "tree", "--collection", collection, "--group", config.group]
    if config.dest_root:
        tree_cmd += ["--dest-root", config.dest_root]
    tree = exec(tree_cmd)
    context.log.info(f"Rebuilt tree for {config.group}/{collection}: {tree.get('items')} item(s)")
    return dg.Output({**result, "tree": tree})


@dg.asset(
    group_name="ingest",
)
def reconcile_collection_items(context: dg.AssetExecutionContext, config: ReconcileCollectionConfig):
    """Reconcile a collection's bronze item list against its source.

    Calls the digtk CLI to (re)ingest from the collection's source, then diffs the
    returned item list + content hashes against the collection's previous
    bronze state (its `latest`, or most recent dated/keyed, manifest). When
    items were added, the collection's 'collections' partition is registered
    in this same run. Item changes reach the gold pipeline through
    bronze_collection_cask_audit, not through partitions.

    Preservica sources are targeted, not a full re-ingest: `config.preservica_changed`
    (populated by bronze_preservica_auto_sync from
    digtk.models.collection.find_changed_preservica_collections()) names the
    specific folder(s)/item(s) that actually changed, and only those are
    synced — see digtk.models.collection.preservica_sync_entities(). A brand
    new collection's initial full import is out of scope here; that's still
    a manual `digtk preservica import` (see that command's own docs).

    Not partitioned itself: the collection is taken from config.

    :param context: Dagster asset execution context.
    :param config: ReconcileCollectionConfig with group, collection, source_type,
        source_uri (non-preservica sources) / preservica_changed (preservica).
    :returns: Dict with the collection key and added/removed/changed ARK lists.
    """
    collection_key = f"bronze/{config.group}/{config.collection}"

    preservica_cmd = ["digtk", "preservica", "sync"]
    for entity in config.preservica_changed:
        preservica_cmd += ["--changed", f"{entity.type}:{entity.ref}"]

    cmd_map = {
        "website": ["digtk", "ingest", "crawl", config.source_uri],
        "db-dump": ["digtk", "ingest", "db-dump", config.source_uri],
        "digital-collection": ["digtk", "ingest", "batch", config.source_uri],
        "preservica": preservica_cmd,
    }
    cmd = cmd_map[config.source_type] + ["--collection", config.collection, "--group", config.group]
    result = exec(cmd)

    added = result.get("added", [])
    removed = result.get("removed", [])
    changed = result.get("changed", [])

    if added:
        context.instance.add_dynamic_partitions("collections", [collection_key])

    context.log.info(
        f"Reconciled {collection_key}: +{len(added)} added, -{len(removed)} removed, ~{len(changed)} changed"
    )
    return dg.Output({
        "collection": collection_key, "source_type": config.source_type,
        "added": added, "removed": removed, "changed": changed,
    })


@dg.asset(
    deps=[reconcile_collection_items],
    group_name="ingest",
)
def rebuild_collection_tree(context: dg.AssetExecutionContext, config: RebuildTreeConfig):
    """Update bronze's tree/ (human-readable, title-keyed) layout for a
    Preservica collection from its just-synced by-ref/ state — no network
    calls (see digtk.models.collection.preservica_tree()/preservica_tree_partial()).

    Runs immediately after reconcile_collection_items in the same job, so
    tree/ stays current after every incremental Preservica sync (see
    bronze_preservica_auto_sync) instead of only after a manual `digtk
    preservica tree` run. A no-op for the other three source types — tree/
    is derived purely from by-ref/, and only Preservica writes by-ref/.

    When config.preservica_changed is non-empty (the sensor always populates
    it with the same changed entities passed to reconcile_collection_items),
    only those entities' tree/ entries are patched in place
    (preservica_tree_partial()) — this both fixes the rename case (a targeted
    sync never refreshes a parent's cached children.json, so a full rebuild
    alone can't be trusted to pick up a new title/slug for an unrelated
    ref — see preservica_tree_partial()'s docstring) and avoids a full
    delete-and-rebuild of tree/ on every sync for large collections.
    preservica_changed is left empty for a full preservica_tree() rebuild —
    e.g. a manually-triggered run, or right after `preservica import`.

    :param context: Dagster asset execution context.
    :param config: RebuildTreeConfig with group, collection, source_type,
        preservica_changed.
    """
    if config.source_type != "preservica":
        context.log.info(
            f"Skipping tree rebuild for {config.group}/{config.collection}: "
            f"source_type={config.source_type!r} has no by-ref/ to rebuild from."
        )
        return dg.Output({"skipped": True})

    if config.preservica_changed:
        cmd = ["digtk", "preservica", "tree", "--collection", config.collection, "--group", config.group]
        for entity in config.preservica_changed:
            cmd += ["--changed", f"{entity.type}:{entity.ref}"]
        result = exec(cmd)
        context.log.info(
            f"Patched tree for {config.group}/{config.collection}: "
            f"+{len(result.get('added', []))} added, -{len(result.get('removed', []))} removed, "
            f"~{len(result.get('moved', []))} moved"
        )
        return dg.Output(result)

    result = exec(["digtk", "preservica", "tree", "--collection", config.collection, "--group", config.group])
    context.log.info(f"Rebuilt tree for {config.group}/{config.collection}: {result.get('items')} item(s)")
    return dg.Output(result)
