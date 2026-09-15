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
    """
    lang: str = Field("eng", description="Tesseract language code.")
    psm: int = Field(1,     description="Page segmentation mode.")
    oem: int = Field(3,     description="OCR engine mode.")


class SearchIndexConfig(Config):
    """Config for the index_for_rag asset.

    :param str app: Gold app to read from when indexing.
    """
    app: GOLD_APPS = Field("arda", description="Gold app to index from.")
