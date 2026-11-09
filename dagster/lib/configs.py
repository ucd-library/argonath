"""
Dagster config schemas and partition definitions for the Argonath pipeline.

Collection jobs are partitioned by collection, workspace jobs by workspace
folder. Item jobs are unpartitioned and address the item by its bronze folder.
"""
import dagster as dg
from dagster import Config
from pydantic import Field
from typing import Literal

from lib.fanout_batching import DEFAULT_MAX_STEPS


# ---------------------------------------------------------------------------
# Partition definitions
# ---------------------------------------------------------------------------

# Collection dimension — keyed "bronze/[group]/[collection]" (e.g.
# "bronze/preservica/haeger"), matching the collection's CaskFS bronze path.
# Group is folded into the key so identically-named collections in different
# groups (e.g. a "website-harvests" collection and a "digital-collections"
# collection that happen to share a name) don't collide. Partitions registered
# under the older "[group]/[collection]" form aren't removed automatically.
collections_partitions = dg.DynamicPartitionsDefinition(name="collections")

# Workspace folders — keyed "workspace/[username]/[folder]" (e.g.
# "workspace/jdoe/box-12"), i.e. cask:/workspace/[username]/[folder]: scans
# staged before they have an ARK or a bronze copy (see digtk.models.workspace).
# Kept in sync with CaskFS by lib.sensors.workspace_partition_sync; used by
# lib.pipeline_workspace's workspace_process.
workspace_partitions = dg.DynamicPartitionsDefinition(name="workspace")


# ---------------------------------------------------------------------------
# Config schemas
# ---------------------------------------------------------------------------

# "" = no enhancement, see image_magick.prep_ocr.
OCR_ENHANCE = Literal["", "auto", "gray"]
TAG_MODE = Literal["auto", "inplace", "rebuild"]
GEOMETRY = Literal["tesseract", "qwen"]


class IngestCollectionConfig(Config):
    """Config for batch collection ingestion.

    :param str group: Group namespace the collection belongs to (bronze path segment).
    :param str collection: Collection name.
    :param str directory: Source directory of files to ingest.
    """
    group: str = Field(..., description="Group namespace the collection belongs to.")
    collection: str = Field(..., description="Collection name.")
    directory: str = Field(..., description="Source directory for batch ingest.")


class PreservicaChangedEntity(Config):
    """One changed Preservica entity to sync — see
    digtk.models.collection.preservica_sync_entities().

    :param str ref: Preservica entity ref.
    :param str type: 'SO' (structural object/folder) or 'IO' (information object/item).
    """
    ref: str = Field(..., description="Preservica entity ref.")
    type: Literal["SO", "IO"] = Field(..., description="Entity type.")


class ReconcileCollectionConfig(Config):
    """Config for the reconcile_collection_items asset.

    Triggers a source-specific ingest (website crawl, DB dump, or a one-shot
    digital-collection batch) and reconciles the returned bronze item list
    against the collection's previous state. Digital-collection sources are
    additive-only — items missing from a re-run are never auto-deleted; website
    and db-dump sources are fully reconciled, so missing items are pruned.

    Preservica sources are handled differently from the other three: rather
    than a full re-ingest, only the specific entities in `preservica_changed`
    are synced (see digtk.models.collection.preservica_sync_entities()) —
    never a full recursive re-walk of the collection.

    :param str group: Group namespace the collection belongs to.
    :param str collection: Collection name.
    :param str source_type: Ingestion source — "website", "db-dump",
        "digital-collection", or "preservica".
    :param str|None source_uri: Crawl root URL or DB connection string — used
        by the website/db-dump/digital-collection sources. Not used for
        source_type="preservica" (see preservica_changed).
    :param list[PreservicaChangedEntity] preservica_changed: Changed
        Preservica entities to sync — only used when source_type="preservica".
        Populated by bronze_preservica_auto_sync from
        digtk.models.collection.find_changed_preservica_collections()'s
        per-collection 'changed' list.
    """
    group: str = Field(..., description="Group namespace the collection belongs to.")
    collection: str = Field(..., description="Collection name.")
    source_type: Literal["website", "db-dump", "digital-collection", "preservica"] = Field(
        ..., description="Ingestion source type."
    )
    source_uri: str | None = Field(None, description="Crawl root URL or DB connection string.")
    preservica_changed: list[PreservicaChangedEntity] = Field(
        default_factory=list,
        description="Changed Preservica entities to sync (source_type='preservica' only).",
    )


class PreservicaImportConfig(Config):
    """Config for the preservica_import_collection asset.

    `collection` is required and taken as plain manual input: unlike the other
    Preservica configs (ReconcileCollectionConfig etc.), there's no
    collections_partitions key to read it from yet, since the whole point of
    this asset is bringing a brand-new collection into bronze for the first
    time (see digtk.models.collection.preservica_import()). Once `digtk
    preservica import` registers it, collection_partition_sync_sensor adds the
    'collections' dynamic partition on its next tick, and ongoing changes go
    through reconcile_collection_items/preservica_auto_sync_sensor instead.

    :param str ref: Root Preservica structural object ref to walk, or (if not a
        ref/UUID) a '/'-separated path of folder titles under the tenant root
        to resolve automatically, e.g. 'haeger' or '/haeger/sub-folder' — see
        `digtk preservica list` to discover exact titles.
    :param str collection: Collection name (manual input; this collection has
        no Dagster partition yet).
    :param str group: Bronze group namespace. Defaults to 'preservica'.
    :param int max_depth: Maximum folder recursion depth. Defaults to 1 here
        (unlike `digtk preservica import`'s own CLI default of unlimited), so
        a first run stays shallow until the collection's shape is known.
    :param str dest_root: Override the bronze root — see `digtk preservica
        import --dest-root`'s own help.
    :param str source_dir: Read from a local directory instead of the live
        Preservica API — see `digtk preservica import --source-dir`'s own help.
    :param bool skip_unchanged: Skip re-downloading a file whose Preservica-
        reported fixity already matches the local copy. Defaults to True here
        (opt-out), unlike the CLI's own off-by-default; uncheck it for a run
        that must re-fetch every file regardless of fixity.
    """
    ref: str = Field(
        ..., description="Root Preservica structural object ref to walk, or a '/'-separated "
        "path of folder titles (e.g. 'haeger' or '/haeger/sub-folder') to resolve automatically.",
    )
    collection: str = Field(..., description="Collection name (manual input; not yet a partition).")
    group: str = Field("preservica", description="Bronze group namespace.")
    max_depth: int = Field(1, description="Maximum folder recursion depth.")
    dest_root: str = Field("", description="Override the bronze root.")
    source_dir: str = Field("", description="Read from a local directory instead of the live API.")
    skip_unchanged: bool = Field(True, description="Skip re-downloading files whose fixity is unchanged.")


class RebuildTreeConfig(Config):
    """Config for the rebuild_collection_tree asset.

    :param str group: Group namespace the collection belongs to.
    :param str collection: Collection name.
    :param str source_type: Same source type as the reconcile step that ran
        just before this one — tree/ is only rebuilt for "preservica" sources
        (see digtk.models.collection.preservica_tree()); any other source_type
        is a no-op.
    :param list[PreservicaChangedEntity] preservica_changed: Same changed
        Preservica entities passed to the matching reconcile_collection_items
        run (see ReconcileCollectionConfig.preservica_changed). When non-empty,
        only these entities' tree/ entries are patched in place
        (digtk.models.collection.preservica_tree_partial()) instead of a full
        rebuild. Left empty for a full preservica_tree() rebuild — e.g. a
        manually-triggered run, or the initial rebuild after `preservica import`.
    """
    group: str = Field(..., description="Group namespace the collection belongs to.")
    collection: str = Field(..., description="Collection name.")
    source_type: Literal["website", "db-dump", "digital-collection", "preservica"] = Field(
        ..., description="Ingestion source type."
    )
    preservica_changed: list[PreservicaChangedEntity] = Field(
        default_factory=list,
        description="Changed Preservica entities to patch tree/ for (source_type='preservica' only).",
    )


class ToolStepsConfig(Config):
    """Which `digtk workspace run` tool steps to run, plus each step's options.
    Base of WorkspaceRunConfig (outputs to the workspace folder's derivatives/).

    Every step is off by default. Steps always run in
    digtk.models.workspace.STEP_ORDER regardless of which are ticked;
    split_detect and split_apply are meant to be separate runs, with a human
    reviewing gutters.json in between. Steps that don't apply to an item
    (e.g. tag-pdf on an images-only folder) are skipped, and a step that fails
    doesn't stop the item's remaining steps — see digtk.models.workspace.run().

    :param bool split_detect: Run split-spreads detect.
    :param bool split_apply: Run split-spreads apply.
    :param bool ocr: Run Tesseract + vision OCR.
    :param bool ocr_correct: Correct OCR words from the vision transcript.
    :param bool alt_text: Generate alt text.
    :param bool pdf_build: Build image folders into one tagged PDF.
    :param bool tag_pdf: Build tagged PDF/UA-1 PDFs (PDF items only).
    :param int max_px: split-detect render width.
    :param str lang: Tesseract language code.
    :param int psm: Page segmentation mode.
    :param int oem: OCR engine mode.
    :param bool vision: Also run Qwen vision OCR for the transcript.
    :param str ocr_enhance: ocr/tag-pdf contrast boost ("", auto, gray).
    :param str collection_context: alt-text prompt context.
    :param bool want_description: Also generate a catalog description.
    :param bool want_long: Long descriptions for information-dense images.
    :param int max_alt_chars: Maximum alt-text length.
    :param str doc_lang: tag-pdf document language (BCP-47).
    :param bool tables: tag-pdf vision layout pass for tables.
    :param bool strict: tag-pdf demotes unmatched lines to artifacts.
    :param int lookahead: tag-pdf layout match lookahead.
    :param bool paragraphs: tag-pdf one /P per layout text block.
    :param bool reuse_ocr: tag-pdf reuses the ocr step's word boxes.
    :param bool correct: tag-pdf fixes spelling from the transcript.
    :param bool skip_existing: Skip pages and documents already done.
    :param bool ocr_text_pages: ocr also OCRs PDF pages with text.
    :param str mode: tag-pdf mode: auto, inplace or rebuild.
    :param bool embed_fonts: tag-pdf in place embeds missing fonts.
    :param bool figures: Run the figures step and tag figures.
    :param str geometry: tag-pdf/pdf-build text layer source.
    :param float min_conf: tag-pdf minimum hOCR word confidence (0-100).
    :param bool validate: Run veraPDF on each tagged PDF.
    """
    split_detect: bool = Field(False, description="Run split-spreads detect.")
    split_apply: bool = Field(False, description="Run split-spreads apply (re-run alone after correcting gutters.json).")
    ocr: bool = Field(False, description="Run Tesseract + vision OCR.")
    ocr_correct: bool = Field(False, description="Correct OCR words from the vision transcript.")
    alt_text: bool = Field(False, description="Generate alt text.")
    pdf_build: bool = Field(False, description="Build each image folder into one tagged PDF (after the file steps).")
    tag_pdf: bool = Field(False, description="Build tagged PDF/UA-1 PDFs (PDF items only).")
    max_px: int = Field(1400, description="split-detect: render width, in pixels.")
    lang: str = Field("eng", description="ocr: Tesseract language code.")
    psm: int = Field(1, description="ocr: page segmentation mode.")
    oem: int = Field(3, description="ocr: OCR engine mode.")
    vision: bool = Field(False, description="ocr: also run Qwen vision OCR for the transcript.")
    ocr_enhance: OCR_ENHANCE = Field("", description="ocr/tag-pdf: contrast boost for faded originals.")
    collection_context: str = Field("", description="alt-text: collection-specific prompt context.")
    want_description: bool = Field(False, description="alt-text: also generate a catalog description.")
    want_long: bool = Field(False, description="alt-text: long descriptions for information-dense images.")
    max_alt_chars: int = Field(150, description="alt-text: maximum alt-text length.")
    doc_lang: str = Field("en-US", description="tag-pdf: document language.")
    tables: bool = Field(False, description="tag-pdf: vision layout pass for Table/TR/TD structure.")
    strict: bool = Field(False, description="tag-pdf: demote unmatched lines to artifacts.")
    lookahead: int = Field(40, description="tag-pdf: how far a layout block may match past the cursor.")
    paragraphs: bool = Field(False, description="tag-pdf: one paragraph per layout text block (needs tables).")
    reuse_ocr: bool = Field(False, description="tag-pdf: reuse the ocr step's word boxes when they match.")
    correct: bool = Field(False, description="tag-pdf: fix text-layer spelling from the vision transcript.")
    skip_existing: bool = Field(False, description="Skip pages and documents whose outputs already exist.")
    ocr_text_pages: bool = Field(False, description="ocr: also OCR PDF pages that already have text.")
    mode: TAG_MODE = Field("auto", description="tag-pdf: auto (in place if any page has text), inplace, rebuild.")
    embed_fonts: bool = Field(False, description="tag-pdf: embed missing fonts with Ghostscript when tagging in place.")
    figures: bool = Field(False, description="Detect and describe in-page figures, and tag them in tag-pdf/pdf-build.")
    geometry: GEOMETRY = Field("tesseract", description="tag-pdf/pdf-build text layer: tesseract, or qwen for handwriting.")
    min_conf: float = Field(0.0, description="tag-pdf: drop hOCR words below this confidence (0-100).")
    validate: bool = Field(False, description="tag-pdf: run veraPDF on the tagged PDF.")


class WorkspaceRunConfig(ToolStepsConfig):
    """Config for lib.pipeline_workspace.workspace_process's fan_out_workspace
    op: ToolStepsConfig plus the fan-out's step cap.

    :param int max_steps: Most steps each fan-out (files, folders) may produce;
        past that, several share a step (see lib.fanout_batching). 0 = never batch.
    """
    max_steps: int = Field(
        DEFAULT_MAX_STEPS, description="Most steps per fan-out; more files/folders are batched into shared steps. 0 = never batch.",
    )


class DerivativesConfig(Config):
    """Thumbnail widths; base of CollectionDerivativesConfig.

    :param str widths: Comma-separated thumbnail widths.
    """
    widths: str = Field("256,512,1024", description="Thumbnail widths in pixels.")


class CollectionDerivativesConfig(DerivativesConfig):
    """DerivativesConfig plus the fan-out's step cap. Base of ItemRunConfig
    (lib.pipeline_item.gold_dcs_process_item).

    :param str widths: Comma-separated thumbnail widths.
    :param int max_steps: Most steps each fan-out (per-file derivatives,
        per-item finalize) may produce; past that, several files/items share a
        step (see lib.fanout_batching). 0 = never batch.
    """
    max_steps: int = Field(
        DEFAULT_MAX_STEPS, description="Most steps per fan-out; more files are batched into shared steps. 0 = never batch.",
    )


class ItemRunConfig(CollectionDerivativesConfig):
    """Config for lib.pipeline_item.gold_dcs_process_item — one item, addressed by
    its bronze tree folder, plus CollectionDerivativesConfig's widths/max_steps.

    `item` defaults to "" only because lib.utils.default_run_config() builds
    the launchpad defaults with `cls()`; pdc_plan_item rejects an empty value.

    :param str widths: Comma-separated thumbnail widths.
    :param int max_steps: Most derivative steps the item's files may
        produce; past that, several files share a step (see lib.fanout_batching). 0 = never batch.
    :param str item: The item's bronze folder, e.g.
        "bronze/preservica/haeger/latest/tree/quarter-mile" ('cask:/' and a
        leading '/' are accepted too).
    :param bool index_es: Index the item into the showcase Elasticsearch index
        (`digtk showcase es upsert`) once it's finalized.
    :param bool force: Regenerate derivatives that already exist.
    """
    item: str = Field(
        "", description="Bronze folder of the item, e.g. bronze/preservica/haeger/latest/tree/quarter-mile.",
    )
    index_es: bool = Field(True, description="Index the item into the showcase Elasticsearch index afterwards.")
    force: bool = Field(False, description="Regenerate derivatives that already exist (default: skip them).")


class ProcessCollectionConfig(Config):
    """Config for lib.pipeline_collection.gold_dcs_process_collection.

    :param bool force: Queue every item with force set, so existing
        derivatives are regenerated instead of skipped.
    """
    force: bool = Field(False, description="Regenerate derivatives that already exist (default: skip them).")


class ReindexCollectionConfig(Config):
    """Config for lib.pipeline_index.gold_dcs_reindex_collection.

    :param bool rebuild: Rebuild each gold item record before indexing.
    :param bool prune: Delete indexed items no longer in bronze.
    :param bool force_prune: Skip the prune safety guard.
    :param int batch_size: Items per step (one bulk request each).
    """
    rebuild: bool = Field(False, description="Rebuild each gold item record (build-item) before indexing.")
    prune: bool = Field(True, description="Delete indexed items no longer in the collection's bronze items.")
    force_prune: bool = Field(False, description="Skip the prune guard (no items in bronze, or >50% deleted).")
    batch_size: int = Field(200, description="Items per step; each step sends one bulk request.")


class CaskChangesConfig(Config):
    """Config for lib.pipeline_index.gold_dcs_apply_cask_changes, filled in by
    bronze_collection_cask_audit. Items to process are queued as gold_dcs_process_item runs instead.

    :param list[str] reindex: Bronze item folders to rebuild and reindex.
    :param list[str] delete: Item ARKs to delete from the showcase index.
    :param list[str] cleanup: Deleted bronze item folders whose silver/gold to remove.
    :param int batch_size: Items per reindex step.
    """
    reindex: list[str] = Field([], description="Bronze item folders to rebuild and reindex.")
    delete: list[str] = Field([], description="Item ARKs to delete from the showcase index.")
    cleanup: list[str] = Field([], description="Deleted bronze item folders whose silver/gold to remove.")
    batch_size: int = Field(200, description="Items per reindex step.")


class UnpublishCollectionConfig(Config):
    """Config for lib.pipeline_index.gold_dcs_unpublish_collection.

    :param bool purge_silver: Also delete the items' silver website derivatives.
    """
    purge_silver: bool = Field(
        False, description="Also delete silver website derivatives (kept while another gold project has the collection).",
    )


class DeleteItemIndexConfig(Config):
    """Config for lib.pipeline_index.gold_dcs_delete_item_index.

    `ark` defaults to "" only so default_run_config() can build launchpad defaults.

    :param str ark: Item ARK to delete from the showcase indexes.
    :param str collection: Collection key to refresh, if the item can't be resolved.
    """
    ark: str = Field("", description="Item ARK, e.g. ark:/87287/d7abc1234.")
    collection: str = Field(
        "", description="Collection key to refresh afterwards when the item is gone from cask, e.g. bronze/preservica/haeger.",
    )
