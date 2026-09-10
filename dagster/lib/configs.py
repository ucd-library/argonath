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


class ReconcileCollectionConfig(Config):
    """Config for the reconcile_collection_items asset.

    Triggers a source-specific ingest (website crawl, DB dump, or a one-shot
    digital-collection batch) and reconciles the returned bronze item list
    against the collection's previous state, registering or deleting Dagster
    dynamic partitions to match. Digital-collection sources are additive-only —
    items missing from a re-run are never auto-deleted; website and db-dump
    sources are fully reconciled, so missing items are pruned.

    :param str group: Group namespace the collection belongs to.
    :param str collection: Collection name.
    :param str source_type: Ingestion source — "website", "db-dump", or "digital-collection".
    :param str source_uri: Crawl root URL or DB connection string, depending on source_type.
    """
    group: str = Field(..., description="Group namespace the collection belongs to.")
    collection: str = Field(..., description="Collection name.")
    source_type: Literal["website", "db-dump", "digital-collection"] = Field(
        ..., description="Ingestion source type."
    )
    source_uri: str = Field(..., description="Crawl root URL or DB connection string.")


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
