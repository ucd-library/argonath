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
    SplitSpreadsConfig,
    RebuildTreeConfig,
    DerivativesConfig,
    OcrConfig,
    SearchIndexConfig,
    AltTextConfig,
    TagPdfConfig,
)
from lib.utils import exec


def _log_failures(context, result):
    """Surface the pages a digtk tool skipped (status "partial" — see e.g.
    digtk.models.ocr.run_ocr()) as warnings, so a partial run is visible in the
    event log instead of only in the raw stdout.

    :param context: Dagster asset execution context.
    :param dict result: The digtk command's parsed JSON result.
    """
    for failure in result.get("failed") or []:
        page = f" page {failure['page']}" if failure.get("page") else ""
        context.log.warning(f"{failure['file']}{page} {failure['stage']} failed -> {failure['error']}")


def _reporting_args(context):
    """--reporting-job-id/--reporting-collection flags for a per-item asset's
    digtk call, so each run records a digtk.lib.reporting row the same way
    lib.pipeline_fanout's ops do.

    :param context: Dagster asset execution context, partitioned by items_partitions.
    :returns: argv fragment.
    :rtype: list[str]
    """
    collection_key = context.partition_key.keys_by_dimension["collection"]
    return ["--reporting-job-id", context.run_id, "--reporting-collection", collection_key]


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
        context.instance.add_dynamic_partitions("items", added)

    if removed and config.source_type != "digital-collection":
        for ark in removed:
            context.instance.delete_dynamic_partition("items", ark)

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
    preservica_auto_sync_sensor) instead of only after a manual `digtk
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


@dg.asset(
    partitions_def=items_partitions,
    group_name="ingest",
)
def ingest_item(context: dg.AssetExecutionContext, config: IngestItemConfig):
    """Perform per-item ingest bookkeeping for an already-discovered bronze item.

    The item's ARK is read and its partition registered upstream, by
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
    group_name="ingest",
)
def detect_spreads(context: dg.AssetExecutionContext, config: SplitSpreadsConfig):
    """Detect the binding gutter in every not-yet-detected image binary of an item.

    Reads the item's bronze image binaries directly — depends on ingest_item
    (bronze existing), not generate_derivatives, since it doesn't need any
    display/OCR-prep derivative to already exist. Publishes review strips +
    a contact sheet as silver derivatives of this item for a human to check
    before apply_spreads runs.

    :param context: Dagster asset execution context.
    :param config: SplitSpreadsConfig with max_px.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    cmd = ["digtk", "split-spreads", "detect", ark, "--max-px", str(config.max_px)]
    result = exec(cmd + _reporting_args(context))
    context.log.info(f"Detected spreads for {ark}: {result}")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    deps=[detect_spreads],
    group_name="ingest",
)
def apply_spreads(context: dg.AssetExecutionContext):
    """Cut every confirmed spread per this item's (human-reviewed) gutters.json.

    The deps=[detect_spreads] declaration is for asset-graph accuracy only
    (apply reads what detect produces) — nothing here auto-chains the two;
    an operator triggers apply_spreads_job manually after confirming a human
    has reviewed/corrected gutters.json.

    :param context: Dagster asset execution context.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    cmd = ["digtk", "split-spreads", "apply", ark]
    result = exec(cmd + _reporting_args(context))
    context.log.info(f"Applied spread splits for {ark}: {result}")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    deps=[ingest_item],
    group_name="derivatives",
)
def generate_derivatives(context: dg.AssetExecutionContext, config: DerivativesConfig):
    """Generate the full image-derivative set for an ingested item: thumbnails,
    a web JPEG, and an OCR-prep image, sharing one scan-orientation decision.

    `digtk showcase image derivatives` auto-detects orientation via a real Tesseract
    comparison (deskewed vs. non-deskewed) rather than a fixed rule, and
    applies that single decision consistently across every output — see
    docs/cask-conventions.md's "Scan Orientation" section.

    Assumes this partition's item is an image item — there's no Dagster-level
    routing by media type yet. PDF/video/audio items are processed by invoking
    `digtk showcase pdf/video/audio derivatives` directly (see docs/cask-conventions.md's
    "Multi-Format Items" section); wiring that into Dagster is a deliberately
    separate, not-yet-scoped piece of work.

    :param context: Dagster asset execution context.
    :param config: DerivativesConfig with thumbnail widths.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    result = exec(["digtk", "showcase", "image", "derivatives", ark, "--widths", config.widths])
    context.log.info(f"Derivatives result: {result}")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    deps=[ingest_item],
    group_name="ocr",
)
def run_ocr(context: dg.AssetExecutionContext, config: OcrConfig):
    """Run OCR on every image/PDF page of an item.

    Both engines run unconditionally: Tesseract for word geometry, Qwen
    vision for the authoritative transcript — not a confidence-gated
    fallback, see digtk.models.ocr's module docstring. Depends on
    ingest_item, not generate_derivatives: `digtk ocr tesseract` prepares its
    own inputs from the bronze binaries (see digtk.models.prep).

    :param context: Dagster asset execution context.
    :param config: OcrConfig with lang, psm, oem, vision.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    cmd = ["digtk", "ocr", "tesseract", ark,
           "--lang", config.lang,
           "--psm",  str(config.psm),
           "--oem",  str(config.oem)]
    if not config.vision:
        cmd.append("--no-vision")
    result = exec(cmd + _reporting_args(context))
    _log_failures(context, result)
    context.log.info(f"OCR result: {result}")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    deps=[ingest_item],
    group_name="accessibility",
)
def generate_alt_text(context: dg.AssetExecutionContext, config: AltTextConfig):
    """Generate AI alt text for an item's image(s)/PDF page(s) using Samwise vision.

    Depends only on ingest_item: `digtk accessibility alt-text` builds its
    own resized image per image/PDF page from the bronze binaries (see
    digtk.models.prep) — it needs neither showcase derivatives, OCR text,
    nor the (still-unimplemented, phase 0.6) AI metadata draft.

    :param context: Dagster asset execution context.
    :param config: AltTextConfig with collection_context, want_description, max_alt_chars.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    cmd = ["digtk", "accessibility", "alt-text", ark, "--max-alt-chars", str(config.max_alt_chars)]
    if config.collection_context:
        cmd += ["--collection-context", config.collection_context]
    if config.want_description:
        cmd += ["--want-description"]
    result = exec(cmd + _reporting_args(context))
    _log_failures(context, result)
    context.log.info(f"Alt-text result: {result}")
    return dg.Output(result)


@dg.asset(
    partitions_def=items_partitions,
    deps=[ingest_item],
    group_name="accessibility",
)
def tag_accessible_pdf(context: dg.AssetExecutionContext, config: TagPdfConfig):
    """Assemble a searchable, PDF/UA-1-tagged PDF for an item's PDF binary(ies).

    Depends only on ingest_item: `digtk accessibility tag-pdf` rasterizes
    each page and builds its own OCR-prep images from the bronze PDF (see
    digtk.models.prep). Optionally reads back generate_alt_text's output for
    richer /Figure tagging, but doesn't require it — see
    digtk.models.accessibility.tag_pdf's own docstring.

    :param context: Dagster asset execution context.
    :param config: TagPdfConfig with lang, strict, tables, validate.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    cmd = ["digtk", "accessibility", "tag-pdf", ark, "--lang", config.lang]
    if config.strict:
        cmd.append("--strict")
    if config.tables:
        cmd.append("--tables")
    if not config.validate:
        cmd.append("--no-validate")
    result = exec(cmd + _reporting_args(context))
    context.log.info(f"PDF tagging result: {result}")
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
    group_name="showcase_search",
)
def index_for_showcase_search(context: dg.AssetExecutionContext):
    """Index an item's schema.org metadata into the showcase website's
    Elasticsearch index - the same index fin's dbsync populates today (see
    dams/services/fin/models/item/{model,transform}.js in the dams repo).

    Deliberately depends on generate_derivatives, not generate_metadata: the
    metadata phase's AI-drafted output is meant for human review before
    publication, and this asset feeds the live public-facing search index -
    it should only ever index reviewed/structural metadata, never an
    unreviewed AI draft. Revisit this dependency if/when a "metadata
    approved" step is added to the pipeline.

    Distinct from index_for_rag above, which feeds an unrelated
    LlamaIndex/pgvector RAG search space - named index_for_showcase_search
    rather than overloading index_for_rag's name so the two indexing
    concerns (public website search vs. AI RAG search) don't get conflated.

    :param context: Dagster asset execution context.
    """
    ark = context.partition_key.keys_by_dimension["item"]
    result = exec(["digtk", "showcase", "es", "upsert", ark])
    context.log.info(f"Showcase search index result: {result}")
    return dg.Output(result)
