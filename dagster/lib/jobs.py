"""
Dagster job definitions for the Argonath pipeline.

Jobs group assets for per-phase and full-pipeline runs.
"""
import dagster as dg

from lib.assets import (
    reconcile_collection_items,
    ingest_item,
    generate_derivatives,
    run_ocr,
    generate_metadata,
    index_for_rag,
)

reconcile_collection_job = dg.define_asset_job(
    name="reconcile_collection_job",
    selection=[reconcile_collection_items],
    description="Reconcile a collection's bronze item list against its source and register/prune partitions.",
)

ingest_collection_job = dg.define_asset_job(
    name="ingest_collection_job",
    selection=[ingest_item],
    description="Ingest all items in a collection (batch mode via dc ingest batch).",
)

derivatives_job = dg.define_asset_job(
    name="derivatives_job",
    selection=[generate_derivatives],
    description="Generate image derivatives for all partitioned items.",
)

ocr_job = dg.define_asset_job(
    name="ocr_job",
    selection=[run_ocr],
    description="Run OCR for all partitioned items.",
)

metadata_job = dg.define_asset_job(
    name="metadata_job",
    selection=[generate_metadata],
    description="Generate AI metadata for all partitioned items.",
)

search_index_job = dg.define_asset_job(
    name="search_index_job",
    selection=[index_for_rag],
    description="Index all partitioned items into the RAG store.",
)

full_pipeline_job = dg.define_asset_job(
    name="full_pipeline_job",
    selection=[
        ingest_item,
        generate_derivatives,
        run_ocr,
        generate_metadata,
        index_for_rag,
    ],
    description="Run the full pipeline: ingest → derivatives → OCR → metadata → RAG.",
)
