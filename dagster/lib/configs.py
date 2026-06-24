"""
Dagster config schemas and partition definitions for the Argonath pipeline.

Partitions use DynamicPartitionsDefinition keyed by ARK — one partition per item.
This allows granular per-item re-runs without reprocessing an entire collection.
"""
import dagster as dg
from dagster import Config
from pydantic import Field
from typing import Literal


# ---------------------------------------------------------------------------
# Partition definitions
# ---------------------------------------------------------------------------

# One partition per item, keyed by ARK (e.g. "ark:/87287/d7abc1234")
items_partitions = dg.DynamicPartitionsDefinition(name="items")


# ---------------------------------------------------------------------------
# Config schemas
# ---------------------------------------------------------------------------

APP_SPACES = Literal["arda", "digital-dev", "digital-prod"]


class IngestItemConfig(Config):
    """Config for the ingest_item asset.

    :param str collection: Collection name.
    :param str space: Target app space.
    """
    collection: str = Field(..., description="Collection name.")
    space: APP_SPACES = Field("digital-dev", description="Target app space.")


class IngestCollectionConfig(Config):
    """Config for batch collection ingestion.

    :param str collection: Collection name.
    :param str space: Target app space.
    :param str directory: Source directory of files to ingest.
    """
    collection: str = Field(..., description="Collection name.")
    space: APP_SPACES = Field("digital-dev", description="Target app space.")
    directory: str = Field(..., description="Source directory for batch ingest.")


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

    :param str space: App space to read from when indexing.
    """
    space: APP_SPACES = Field("arda", description="App space to index from.")
