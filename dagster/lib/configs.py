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


# ---------------------------------------------------------------------------
# Partition definitions
# ---------------------------------------------------------------------------

# Collection dimension — keyed "[group]/[collection]" (e.g. "digital-collections/ucd-photos").
# Group is folded into the key so identically-named collections in different
# groups (e.g. a "website-harvests" collection and a "digital-collections"
# collection that happen to share a name) don't collide.
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

# Workspace collections — keyed "workspace/[username]/[collection]" (e.g.
# "workspace/jdoe/box-12"), i.e. cask:/workspace/[username]/[collection]: scans
# staged before they have an ARK or a bronze copy (see digtk.models.workspace).
# Kept in sync with CaskFS by lib.sensors.workspace_partition_sync_sensor; used by
# lib.pipeline_workspace's process_workspace_job and promote_workspace_job.
workspace_partitions = dg.DynamicPartitionsDefinition(name="workspace")


# ---------------------------------------------------------------------------
# Config schemas
# ---------------------------------------------------------------------------

# Gold app targets — used by promotion/indexing configs, not by ingest (ingest
# always lands in bronze; an item only reaches a gold app once promoted).
GOLD_APPS = Literal["arda", "digital-dev", "digital-prod"]


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


class OcrConfig(Config):
    """Config for the run_ocr asset.

    :param str lang: Tesseract language code.
    :param int psm: Page segmentation mode.
    :param int oem: OCR engine mode.
    :param bool vision: Also run Qwen vision OCR for the transcript text.
        Not a quality gate (see digtk.models.ocr's module docstring) — set
        False only for cost control on a large batch.
    """
    lang: str = Field("eng", description="Tesseract language code.")
    psm: int = Field(1,     description="Page segmentation mode.")
    oem: int = Field(3,     description="OCR engine mode.")
    vision: bool = Field(True, description="Also run Qwen vision OCR for the transcript.")


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
    """
    collection_context: str = Field("", description="Collection-specific prompt context.")
    want_description: bool = Field(False, description="Also generate a catalog description.")
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
    """
    lang: str = Field("en-US", description="Document language for tagging.")
    strict: bool = Field(False, description="Demote unaccounted-for lines to artifacts.")
    tables: bool = Field(False, description="Vision layout pass for Table/TR/TD structure.")
    validate: bool = Field(True, description="Run veraPDF on the tagged PDF.")


class WorkspaceRunConfig(Config):
    """Config for lib.pipeline_workspace.process_workspace_job's fan_out_workspace op —
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
    :param bool alt_text: Generate alt text.
    :param bool tag_pdf: Build tagged PDF/UA-1 PDFs (PDF items only).
    :param int max_px: split-detect render width.
    :param str lang: Tesseract language code.
    :param int psm: Page segmentation mode.
    :param int oem: OCR engine mode.
    :param bool vision: Also run Qwen vision OCR for the transcript.
    :param str collection_context: alt-text prompt context.
    :param bool want_description: Also generate a catalog description.
    :param int max_alt_chars: Maximum alt-text length.
    :param str doc_lang: tag-pdf document language (BCP-47).
    :param bool tables: tag-pdf vision layout pass for tables.
    :param float min_conf: tag-pdf minimum hOCR word confidence (0-100).
    :param bool validate: Run veraPDF on each tagged PDF.
    """
    split_detect: bool = Field(True, description="Run split-spreads detect.")
    split_apply: bool = Field(True, description="Run split-spreads apply (re-run alone after correcting gutters.json).")
    ocr: bool = Field(True, description="Run Tesseract + vision OCR.")
    alt_text: bool = Field(True, description="Generate alt text.")
    tag_pdf: bool = Field(True, description="Build tagged PDF/UA-1 PDFs (PDF items only).")
    max_px: int = Field(1400, description="split-detect: render width, in pixels.")
    lang: str = Field("eng", description="ocr: Tesseract language code.")
    psm: int = Field(1, description="ocr: page segmentation mode.")
    oem: int = Field(3, description="ocr: OCR engine mode.")
    vision: bool = Field(True, description="ocr: also run Qwen vision OCR for the transcript.")
    collection_context: str = Field("", description="alt-text: collection-specific prompt context.")
    want_description: bool = Field(True, description="alt-text: also generate a catalog description.")
    max_alt_chars: int = Field(150, description="alt-text: maximum alt-text length.")
    doc_lang: str = Field("en-US", description="tag-pdf: document language.")
    tables: bool = Field(False, description="tag-pdf: vision layout pass for Table/TR/TD structure.")
    min_conf: float = Field(0.0, description="tag-pdf: drop hOCR words below this confidence (0-100).")
    validate: bool = Field(True, description="tag-pdf: run veraPDF on the tagged PDF.")


class PromoteItemConfig(Config):
    """One workspace item to promote, and the ARK it was ingested under.

    :param str source: Item folder under the collection's source/ ("" for
        source/ itself — the usual case, a collection holding a single item).
    :param str ark: The item's ARK in bronze, e.g. "ark:/87287/d3bv7b33r".
    """
    source: str = Field("", description="Item folder under source/ (\"\" = source/ itself).")
    ark: str = Field(..., description="The item's ARK in bronze.")


class PromoteWorkspaceConfig(Config):
    """Config for lib.pipeline_workspace.promote_workspace_job — which reviewed
    workspace items to copy into silver, and under which ARKs (see
    `digtk workspace promote`).

    Three ways to say what to promote, which can be combined:
      - `ark` (+ `source`) for one item — `source` defaults to the collection's
        source/ itself, the case where the collection is a single item;
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
        default_factory=list, description="More {source, ark} pairs, for a collection with several items.",
    )
    ark_from_folder: bool = Field(
        False, description="Promote every item folder as ark:/<naan>/<folder name> (folders named by ARK id).",
    )
    naan: str = Field("", description="ARK NAAN (e.g. 87293), required with ark_from_folder.")
    dry_run: bool = Field(True, description="Only report what would be copied.")
    replace: bool = Field(False, description="Overwrite outputs already in silver.")
