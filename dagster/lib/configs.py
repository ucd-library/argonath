"""
Dagster config schemas and partition definitions for the Argonath pipeline.

Partitions are two-dimensional: one partition per (collection, item) pair.
This allows granular per-item re-runs (via the item dimension) as well as
collection-scoped bulk operations (via the collection dimension) without
reprocessing unrelated collections.
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

# Item dimension — keyed by the item's ARK (e.g. "ark:/87287/d7abc1234").
items_partitions_dim = dg.DynamicPartitionsDefinition(name="items")

# Combined multi-partition — every downstream asset keyed by (collection, item)
# uses this. `context.partition_key.keys_by_dimension["collection" | "item"]`
# retrieves each half.
items_partitions = dg.MultiPartitionsDefinition({
    "collection": collections_partitions,
    "item": items_partitions_dim,
})

# Workspace folders — keyed "workspace/[username]/[folder]" (e.g.
# "workspace/jdoe/box-12"), i.e. cask:/workspace/[username]/[folder]: scans
# staged before they have an ARK or a bronze copy (see digtk.models.workspace).
# Kept in sync with CaskFS by lib.sensors.workspace_partition_sync_sensor; used by
# lib.pipeline_workspace's workspace_process_job and workspace_promote_job.
workspace_partitions = dg.DynamicPartitionsDefinition(name="workspace")


# ---------------------------------------------------------------------------
# Config schemas
# ---------------------------------------------------------------------------

# Gold app targets — used by promotion/indexing configs, not by ingest (ingest
# always lands in bronze; an item only reaches a gold app once promoted).
GOLD_APPS = Literal["arda", "digital-dev", "digital-prod"]
# "" = no enhancement, see image_magick.prep_ocr.
OCR_ENHANCE = Literal["", "auto", "gray"]
TAG_MODE = Literal["auto", "inplace", "rebuild"]
GEOMETRY = Literal["tesseract", "qwen"]


class IngestItemConfig(Config):
    """Config for the ingest_item asset.

    :param str group: Group namespace the collection belongs to (bronze path segment).
    :param str collection: Collection name.
    """
    group: str = Field(..., description="Group namespace the collection belongs to.")
    collection: str = Field(..., description="Collection name.")


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
    against the collection's previous state, registering or deleting Dagster
    dynamic partitions to match. Digital-collection sources are additive-only —
    items missing from a re-run are never auto-deleted; website and db-dump
    sources are fully reconciled, so missing items are pruned.

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
        Populated by preservica_auto_sync_sensor from
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


class SplitSpreadsConfig(Config):
    """Config for the detect_spreads asset (apply_spreads takes no config —
    it only reads back gutters.json, nothing left to configure).

    :param int max_px: Detection render width, in pixels.
    """
    max_px: int = Field(1400, description="Detection render width, in pixels.")


class DerivativesConfig(Config):
    """Config for the generate_derivatives asset.

    :param str widths: Comma-separated thumbnail widths.
    """
    widths: str = Field("256,512,1024", description="Thumbnail widths in pixels.")


class CollectionDerivativesConfig(DerivativesConfig):
    """Config for lib.pipeline_fanout.showcase_process_collection_job — DerivativesConfig
    plus the fan-out's step cap.

    :param str widths: Comma-separated thumbnail widths.
    :param int max_steps: Most steps each fan-out (per-file derivatives,
        per-item finalize) may produce; past that, several files/items share a
        step (see lib.fanout_batching). 0 = never batch.
    """
    max_steps: int = Field(
        DEFAULT_MAX_STEPS, description="Most steps per fan-out; more files are batched into shared steps. 0 = never batch.",
    )


class ItemRunConfig(CollectionDerivativesConfig):
    """Config for lib.pipeline_item.showcase_process_item_job — one item, addressed by
    its bronze tree folder, plus CollectionDerivativesConfig's widths/max_steps.

    `item` defaults to "" only because lib.utils.default_run_config() builds
    the launchpad defaults with `cls()`; pdc_plan_item rejects an empty value.

    :param str widths: Comma-separated thumbnail widths.
    :param int max_steps: Most derivative steps the item's files may produce;
        past that, several files share a step (see lib.fanout_batching). 0 = never batch.
    :param str item: The item's bronze folder, e.g.
        "bronze/preservica/haeger/latest/tree/quarter-mile" ('cask:/' and a
        leading '/' are accepted too).
    :param bool index_es: Index the item into the showcase Elasticsearch index
        (`digtk showcase es upsert`) once it's finalized.
    """
    item: str = Field(
        "", description="Bronze folder of the item, e.g. bronze/preservica/haeger/latest/tree/quarter-mile.",
    )
    index_es: bool = Field(True, description="Index the item into the showcase Elasticsearch index afterwards.")


class ReindexCollectionConfig(Config):
    """Config for lib.pipeline_index.showcase_reindex_collection_job.

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
    """Config for lib.pipeline_index.showcase_apply_cask_changes_job, filled in by
    bronze_cask_audit_sensor. Items to process are queued as showcase_process_item_job runs instead.

    :param list[str] reindex: Bronze item folders to rebuild and reindex.
    :param list[str] delete: Item ARKs to delete from the showcase index.
    :param list[str] cleanup: Deleted bronze item folders whose silver/gold to remove.
    :param int batch_size: Items per reindex step.
    """
    reindex: list[str] = Field([], description="Bronze item folders to rebuild and reindex.")
    delete: list[str] = Field([], description="Item ARKs to delete from the showcase index.")
    cleanup: list[str] = Field([], description="Deleted bronze item folders whose silver/gold to remove.")
    batch_size: int = Field(200, description="Items per reindex step.")


class DeleteItemIndexConfig(Config):
    """Config for lib.pipeline_index.showcase_delete_item_index_job.

    `ark` defaults to "" only so default_run_config() can build launchpad defaults.

    :param str ark: Item ARK to delete from the showcase indexes.
    :param str collection: Collection key to refresh, if the item can't be resolved.
    """
    ark: str = Field("", description="Item ARK, e.g. ark:/87287/d7abc1234.")
    collection: str = Field(
        "", description="Collection key to refresh afterwards when the item is gone from cask, e.g. bronze/preservica/haeger.",
    )


class OcrConfig(Config):
    """Config for the run_ocr asset.

    :param str lang: Tesseract language code.
    :param int psm: Page segmentation mode.
    :param int oem: OCR engine mode.
    :param bool vision: Also run Qwen vision OCR for the transcript text.
        Not a quality gate (see digtk.models.ocr's module docstring) — set
        False only for cost control on a large batch.
    :param str ocr_enhance: Contrast boost for faded originals ("", auto, gray).
    :param bool ocr_text_pages: Also OCR PDF pages that have text.
    """
    lang: str = Field("eng", description="Tesseract language code.")
    psm: int = Field(1,     description="Page segmentation mode.")
    oem: int = Field(3,     description="OCR engine mode.")
    vision: bool = Field(True, description="Also run Qwen vision OCR for the transcript.")
    ocr_enhance: OCR_ENHANCE = Field("", description="Contrast boost for faded originals.")
    ocr_text_pages: bool = Field(False, description="Also OCR PDF pages that already have text.")


class SearchIndexConfig(Config):
    """Config for the index_for_rag asset.

    :param str app: Gold app to read from when indexing.
    """
    app: GOLD_APPS = Field("arda", description="Gold app to index from.")


class AltTextConfig(Config):
    """Config for the generate_alt_text asset.

    collection_context/want_description/max_alt_chars are per-run rather than
    pulled from a persisted per-collection registry — digtk has no such
    registry yet (see digtk.lib.config's module docstring).

    :param str collection_context: Collection-specific prompt context (what this
        collection is), passed through to digtk.models.alt_utils.build_alt_prompt().
    :param bool want_description: Also generate a 2-4 sentence catalog description.
    :param int max_alt_chars: Maximum alt-text length in characters.
    :param bool want_long: Long descriptions for maps, charts, posters.
    """
    collection_context: str = Field("", description="Collection-specific prompt context.")
    want_description: bool = Field(False, description="Also generate a catalog description.")
    want_long: bool = Field(False, description="Long descriptions for information-dense images.")
    max_alt_chars: int = Field(150, description="Maximum alt-text length in characters.")


class FiguresConfig(Config):
    """Config for the detect_figures asset.

    :param str collection_context: Collection-specific alt-text prompt context.
    :param int max_alt_chars: Maximum alt-text length in characters.
    """
    collection_context: str = Field("", description="Collection-specific prompt context.")
    max_alt_chars: int = Field(150, description="Maximum alt-text length in characters.")


class TagPdfConfig(Config):
    """Config for the tag_accessible_pdf asset.

    :param str lang: Document language for tagging (BCP-47, e.g. en-US).
    :param bool strict: Demote lines the layout doesn't account for to
        untagged artifacts instead of paragraphs.
    :param bool tables: Vision layout pass per page: tag data tables as
        Table/TR/TD (one extra Samwise call per page, cached).
    :param bool validate: Run veraPDF on the tagged PDF and include the
        compliance result.
    :param int lookahead: How far a layout block may match past the cursor.
    :param float min_conf: Drop hOCR words below this confidence (0-100).
    :param str ocr_enhance: Contrast boost for faded originals ("", auto, gray).
    :param bool paragraphs: One /P per layout text block (needs tables).
    :param bool reuse_ocr: Reuse run_ocr's word boxes when they match.
    :param bool correct: Fix text-layer spelling from the vision transcript.
    :param bool skip_existing: Skip documents whose outputs already exist.
    :param str mode: tag-pdf only: auto, inplace or rebuild.
    :param bool embed_fonts: tag-pdf in place: embed missing fonts first.
    :param bool figures: Tag in-page figures (detects pages not yet done).
    :param str geometry: Text layer from tesseract or qwen (handwriting).
    """
    lang: str = Field("en-US", description="Document language for tagging.")
    strict: bool = Field(False, description="Demote unaccounted-for lines to artifacts.")
    tables: bool = Field(False, description="Vision layout pass for Table/TR/TD structure.")
    validate: bool = Field(True, description="Run veraPDF on the tagged PDF.")
    lookahead: int = Field(40, description="How far a layout block may match past the cursor.")
    min_conf: float = Field(0.0, description="Drop hOCR words below this confidence (0-100).")
    ocr_enhance: OCR_ENHANCE = Field("", description="Contrast boost for faded originals.")
    paragraphs: bool = Field(False, description="One paragraph per layout text block (needs tables).")
    reuse_ocr: bool = Field(True, description="Reuse run_ocr's word boxes when they match.")
    correct: bool = Field(False, description="Fix text-layer spelling from the vision transcript.")
    skip_existing: bool = Field(False, description="Skip documents whose outputs already exist.")
    mode: TAG_MODE = Field("auto", description="tag-pdf only: auto (in place if any page has text), inplace, rebuild.")
    embed_fonts: bool = Field(True, description="tag-pdf in place: embed missing fonts with Ghostscript first.")
    figures: bool = Field(False, description="Tag in-page figures with alt text (detects pages not yet done).")
    geometry: GEOMETRY = Field("tesseract", description="Text layer: tesseract, or qwen for handwriting.")


class WorkspaceRunConfig(Config):
    """Config for lib.pipeline_workspace.workspace_process_job's fan_out_workspace op —
    which `digtk workspace run` steps to run, plus each step's tuning options
    (the same fields as SplitSpreadsConfig/OcrConfig/AltTextConfig/TagPdfConfig).

    Steps always run in digtk.models.workspace.STEP_ORDER regardless of which
    are ticked; split_detect and split_apply are meant to be separate runs, with
    a human reviewing gutters.json (`digtk workspace pull`) in between.

    By default every step is ticked — split-detect, split-apply, OCR, alt text
    (with a catalog description) and PDF tagging — so a plain launch or a
    backfill (which never sees launchpad edits) does the whole job. That means
    the first split-apply cuts pages from gutters.json before anyone has
    reviewed it; after a review corrects gutters.json, launch again with only
    split_apply ticked to re-cut from the corrected file. Steps that don't apply to an item
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
    :param int max_steps: Most steps each fan-out (files, folders) may produce;
        past that, several share a step (see lib.fanout_batching). 0 = never batch.
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
    max_steps: int = Field(
        DEFAULT_MAX_STEPS, description="Most steps per fan-out; more files/folders are batched into shared steps. 0 = never batch.",
    )


class PromoteItemConfig(Config):
    """One workspace item to promote, and the ARK it was ingested under.

    :param str source: Item folder under the workspace folder's source/ (""
        for source/ itself — the usual case, a folder holding a single item).
    :param str ark: The item's ARK in bronze, e.g. "ark:/87287/d3bv7b33r".
    """
    source: str = Field("", description="Item folder under source/ (\"\" = source/ itself).")
    ark: str = Field(..., description="The item's ARK in bronze.")


class PromoteWorkspaceConfig(Config):
    """Config for lib.pipeline_workspace.workspace_promote_job — which reviewed
    workspace items to copy into silver, and under which ARKs (see
    `digtk workspace promote`).

    Three ways to say what to promote, which can be combined:
      - `ark` (+ `source`) for one item — `source` defaults to the workspace
        folder's source/ itself, the case where the folder is a single item;
      - `items`, a list of {source, ark} pairs;
      - `ark_from_folder` + `naan`, for a batch whose item folders are named
        after their ARK ids (e.g. source/d3bv7b33r -> ark:/87293/d3bv7b33r):
        every item folder in the partition is promoted, no list needed. A
        folder also given explicitly (via `ark`/`source` or `items`) uses that
        ARK instead.

    :param str ark: ARK for the item at `source`. Leave empty to use only `items`
        / `ark_from_folder`.
    :param str source: Item folder under source/ for `ark` ("" = source/ itself).
    :param list[PromoteItemConfig] items: Additional {source, ark} pairs.
    :param bool ark_from_folder: Promote every item folder as ark:/<naan>/<folder name>.
    :param str naan: The ARKs' Name Assigning Authority Number (e.g. "87293") —
        required with ark_from_folder.
    :param bool dry_run: Only report what would be copied (default True —
        check the event log, then launch again with dry_run: false).
    :param bool replace: Overwrite outputs already in silver.
    """
    ark: str = Field("", description="ARK for the item at `source` (the usual single-item case).")
    source: str = Field("", description="Item folder under source/ for `ark` (\"\" = source/ itself).")
    items: list[PromoteItemConfig] = Field(
        default_factory=list, description="More {source, ark} pairs, for a workspace folder with several items.",
    )
    ark_from_folder: bool = Field(
        False, description="Promote every item folder as ark:/<naan>/<folder name> (folders named by ARK id).",
    )
    naan: str = Field("", description="ARK NAAN (e.g. 87293), required with ark_from_folder.")
    dry_run: bool = Field(True, description="Only report what would be copied.")
    replace: bool = Field(False, description="Overwrite outputs already in silver.")
