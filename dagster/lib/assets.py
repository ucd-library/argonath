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

    Not partitioned itself: this asset expands into partitions, it doesn't run
    within one. Upstream of ingest_item and friends, which then run per (collection, item).

    :param context: Dagster asset execution context.
    :param config: ReconcileCollectionConfig with group, collection, source_type, source_uri.
    :returns: Dict with the collection key and added/removed/changed ARK lists.
    """
    collection_key = f"{config.group}/{config.collection}"

    cmd_map = {
        "website": ["digtk", "ingest", "crawl", config.source_uri],
        "db-dump": ["digtk", "ingest", "db-dump", config.source_uri],
        "digital-collection": ["digtk", "ingest", "batch", config.source_uri],
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

    `digtk image derivatives` auto-detects orientation via a real Tesseract
    comparison (deskewed vs. non-deskewed) rather than a fixed rule, and
    applies that single decision consistently across every output — see
    docs/cask-conventions.md's "Scan Orientation" section.

    :param context: Dagster asset execution context.
    :param config: DerivativesConfig with thumbnail widths.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    result = exec(["digtk", "image", "derivatives", ark, "--widths", config.widths])
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
