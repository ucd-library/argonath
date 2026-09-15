"""
Dagster asset definitions for the Argonath pipeline.

Each asset corresponds to one pipeline phase and calls the digtk CLI via exec().
Per-item assets are partitioned by (collection, item) — see lib.configs.items_partitions.
"""
import dagster as dg

from lib.configs import (
    items_partitions,
    IngestItemConfig,
    ReconcileCollectionConfig,
    RebuildTreeConfig,
    DerivativesConfig,
    OcrConfig,
    SearchIndexConfig,
)
from lib.utils import exec


@dg.asset(
    group_name="ingest",
)
def reconcile_collection_items(context: dg.AssetExecutionContext, config: ReconcileCollectionConfig):
    """Reconcile a collection's bronze item list and register/delete partitions to match.

    Calls the digtk CLI to (re)ingest from the collection's source, then diffs the
    returned item list + content hashes against the collection's previous
    bronze state (its `latest`, or most recent dated/keyed, manifest). Newly
    discovered items are registered as dynamic partitions in this same run;
    for website/db-dump sources, items no longer present are pruned (partition
    deleted). Digital-collection sources are additive-only — nothing is ever
    auto-deleted for them.

    Preservica sources are targeted, not a full re-ingest: `config.preservica_changed`
    (populated by preservica_auto_sync_sensor from
    digtk.models.collection.find_changed_preservica_collections()) names the
    specific folder(s)/item(s) that actually changed, and only those are
    synced — see digtk.models.collection.preservica_sync_entities(). A brand
    new collection's initial full import is out of scope here; that's still
    a manual `digtk preservica import` (see that command's own docs).

    Not partitioned itself: this asset expands into partitions, it doesn't run
    within one. Upstream of ingest_item and friends, which then run per (collection, item).

    :param context: Dagster asset execution context.
    :param config: ReconcileCollectionConfig with group, collection, source_type,
        source_uri (non-preservica sources) / preservica_changed (preservica).
    :returns: Dict with the collection key and added/removed/changed ARK lists.
    """
    collection_key = f"{config.group}/{config.collection}"

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
        context.instance.add_dynamic_partitions("items", added)

    if removed and config.source_type != "digital-collection":
        for ark in removed:
            context.instance.delete_dynamic_partition("items", ark)

    context.log.info(
        f"Reconciled {collection_key}: +{len(added)} added, -{len(removed)} removed, ~{len(changed)} changed"
    )
    return dg.Output({"collection": collection_key, "added": added, "removed": removed, "changed": changed})


@dg.asset(
    deps=[reconcile_collection_items],
    group_name="ingest",
)
def rebuild_collection_tree(context: dg.AssetExecutionContext, config: RebuildTreeConfig):
    """Rebuild bronze's tree/ (human-readable, title-keyed) layout for a
    Preservica collection from its just-synced by-ref/ state — no network
    calls (see digtk.models.collection.preservica_tree()).

    Runs immediately after reconcile_collection_items in the same job, so
    tree/ stays current after every incremental Preservica sync (see
    preservica_auto_sync_sensor) instead of only after a manual `digtk
    preservica tree` run. A no-op for the other three source types — tree/
    is derived purely from by-ref/, and only Preservica writes by-ref/.

    :param context: Dagster asset execution context.
    :param config: RebuildTreeConfig with group, collection, source_type.
    """
    if config.source_type != "preservica":
        context.log.info(
            f"Skipping tree rebuild for {config.group}/{config.collection}: "
            f"source_type={config.source_type!r} has no by-ref/ to rebuild from."
        )
        return dg.Output({"skipped": True})

    result = exec(["digtk", "preservica", "tree", "--collection", config.collection, "--group", config.group])
    context.log.info(f"Rebuilt tree for {config.group}/{config.collection}: {result.get('items')} item(s)")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    group_name="ingest",
)
def ingest_item(context: dg.AssetExecutionContext, config: IngestItemConfig):
    """Perform per-item ingest bookkeeping for an already-discovered bronze item.

    The item's ARK is minted and its partition registered upstream, by
    reconcile_collection_items — by the time this asset runs for a given
    partition, the ARK and its bronze location already exist. This asset
    covers any per-item ingest-time work beyond that initial bronze write
    (exact scope TBD in phase 0.3).

    :param context: Dagster asset execution context.
    :param config: IngestItemConfig with group and collection.
    """
    raise NotImplementedError("phase 0.3")


@dg.asset(
    partitions_def=items_partitions,
    deps=[ingest_item],
    group_name="derivatives",
)
def generate_derivatives(context: dg.AssetExecutionContext, config: DerivativesConfig):
    """Generate the full image-derivative set for an ingested item: thumbnails,
    a web JPEG, and an OCR-prep image, sharing one scan-orientation decision.

    `digtk public-dc image derivatives` auto-detects orientation via a real Tesseract
    comparison (deskewed vs. non-deskewed) rather than a fixed rule, and
    applies that single decision consistently across every output — see
    docs/cask-conventions.md's "Scan Orientation" section.

    Assumes this partition's item is an image item — there's no Dagster-level
    routing by media type yet. PDF/video/audio items are processed by invoking
    `digtk public-dc pdf/video/audio derivatives` directly (see docs/cask-conventions.md's
    "Multi-Format Items" section); wiring that into Dagster is a deliberately
    separate, not-yet-scoped piece of work.

    :param context: Dagster asset execution context.
    :param config: DerivativesConfig with thumbnail widths.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    result = exec(["digtk", "public-dc", "image", "derivatives", ark, "--widths", config.widths])
    context.log.info(f"Derivatives result: {result}")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    deps=[generate_derivatives],
    group_name="ocr",
)
def run_ocr(context: dg.AssetExecutionContext, config: OcrConfig):
    """Run OCR on the prepared derivative image for an item.

    Tesseract runs first; low-confidence output triggers ML fallback.

    :param context: Dagster asset execution context.
    :param config: OcrConfig with lang, psm, oem.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    result = exec(["digtk", "ocr", "tesseract", ark,
                   "--lang", config.lang,
                   "--psm",  str(config.psm),
                   "--oem",  str(config.oem)])
    context.log.info(f"OCR result: {result}")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    deps=[run_ocr],
    group_name="metadata",
)
def generate_metadata(context: dg.AssetExecutionContext):
    """Generate AI metadata draft for an item using Samwise (Qwen).

    Writes ai-metadata.jsonld.json to the item's metadata/ process dir.

    :param context: Dagster asset execution context.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    result = exec(["digtk", "metadata", "generate", ark])
    context.log.info(f"Metadata result: {result}")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    deps=[generate_metadata],
    group_name="rag",
)
def index_for_rag(context: dg.AssetExecutionContext, config: SearchIndexConfig):
    """Index an item's OCR text and metadata into the pgvector RAG store.

    :param context: Dagster asset execution context.
    :param config: SearchIndexConfig with target gold app.
    """
    raise NotImplementedError("phase 0.7")


@dg.asset(
    partitions_def=items_partitions,
    deps=[generate_derivatives],
    group_name="dams_search",
)
def index_for_dams_search(context: dg.AssetExecutionContext):
    """Index an item's schema.org metadata into the DAMS website's
    Elasticsearch index - the same index fin's dbsync populates today (see
    dams/services/fin/models/item/{model,transform}.js in the dams repo).

    Deliberately depends on generate_derivatives, not generate_metadata: the
    metadata phase's AI-drafted output is meant for human review before
    publication, and this asset feeds the live public-facing search index -
    it should only ever index reviewed/structural metadata, never an
    unreviewed AI draft. Revisit this dependency if/when a "metadata
    approved" step is added to the pipeline.

    Distinct from index_for_rag above, which feeds an unrelated
    LlamaIndex/pgvector RAG search space - named index_for_dams_search
    rather than overloading index_for_rag's name so the two indexing
    concerns (public website search vs. AI RAG search) don't get conflated.

    :param context: Dagster asset execution context.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    result = exec(["digtk", "public-dc", "es", "upsert", ark])
    context.log.info(f"DAMS search index result: {result}")
    return dg.Output(result)
