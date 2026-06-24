"""
Dagster asset definitions for the Argonath pipeline.

Each asset corresponds to one pipeline phase and calls the dc CLI via exec().
Assets are partitioned by ARK — one partition per item.
"""
import dagster as dg

from lib.configs import (
    items_partitions,
    IngestItemConfig,
    DerivativesConfig,
    OcrConfig,
    SearchIndexConfig,
)
from lib.utils import exec


@dg.asset(
    partitions_def=items_partitions,
    group_name="ingest",
)
def ingest_item(context: dg.AssetExecutionContext, config: IngestItemConfig):
    """Ingest a single item into CaskFS, mint its ARK, write initial sidecar.

    Partition key is the ARK assigned at ingest.

    :param context: Dagster asset execution context.
    :param config: IngestItemConfig with collection and space.
    """
    raise NotImplementedError("phase 0.3")


@dg.asset(
    partitions_def=items_partitions,
    deps=[ingest_item],
    group_name="derivatives",
)
def generate_derivatives(context: dg.AssetExecutionContext, config: DerivativesConfig):
    """Generate image derivatives (thumbnails, web JPEG) for an ingested item.

    :param context: Dagster asset execution context.
    :param config: DerivativesConfig with thumbnail widths.
    """
    ark = context.partition_key
    result = exec(["dc", "image", "thumbnail", ark, "--widths", config.widths])
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
    ark = context.partition_key
    result = exec(["dc", "ocr", "tesseract", ark,
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
    ark = context.partition_key
    result = exec(["dc", "metadata", "generate", ark])
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
    :param config: SearchIndexConfig with target space.
    """
    raise NotImplementedError("phase 0.7")
