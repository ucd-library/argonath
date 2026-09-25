"""
Dagster job definitions for the Argonath pipeline.

Jobs group assets for per-phase and full-pipeline runs.
"""
import dagster as dg

from lib.assets import (
    reconcile_collection_items,
    detect_spreads,
    apply_spreads,
    rebuild_collection_tree,
    ingest_item,
    generate_derivatives,
    run_ocr,
    generate_metadata,
    generate_alt_text,
    tag_accessible_pdf,
    index_for_rag,
    index_for_dams_search,
)

reconcile_collection_job = dg.define_asset_job(
    name="reconcile_collection_job",
    selection=[reconcile_collection_items, rebuild_collection_tree],
    description="Reconcile a collection's bronze item list against its source, register/prune "
    "partitions, and (Preservica sources) rebuild its tree/ layout.",
)

detect_spreads_job = dg.define_asset_job(
    name="detect_spreads_job",
    selection=[detect_spreads],
    description="Detect the binding gutter in an item's image binaries for all partitioned items — "
                "only meaningful for items with scanned two-page spreads, so not part of full_pipeline_job.",
)

apply_spreads_job = dg.define_asset_job(
    name="apply_spreads_job",
    selection=[apply_spreads],
    description="Cut an item's confirmed spreads per its (human-reviewed) gutters.json for all "
                "partitioned items — not auto-chained from detect_spreads_job; run after review.",
)

ingest_collection_job = dg.define_asset_job(
    name="ingest_collection_job",
    selection=[ingest_item],
    description="Ingest all items in a collection (batch mode via digtk ingest batch).",
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

alt_text_job = dg.define_asset_job(
    name="alt_text_job",
    selection=[generate_alt_text],
    description="Generate AI alt text for all partitioned items.",
)

tag_pdf_job = dg.define_asset_job(
    name="tag_pdf_job",
    selection=[tag_accessible_pdf],
    description="Assemble a searchable, PDF/UA-1-tagged PDF for all partitioned "
                "items — only meaningful for PDF items, so not part of full_pipeline_job.",
)

search_index_job = dg.define_asset_job(
    name="search_index_job",
    selection=[index_for_rag],
    description="Index all partitioned items into the RAG store.",
)

dams_search_index_job = dg.define_asset_job(
    name="dams_search_index_job",
    selection=[index_for_dams_search],
    description="Index all partitioned items into the DAMS website's Elasticsearch search index.",
)

full_pipeline_job = dg.define_asset_job(
    name="full_pipeline_job",
    selection=[
        ingest_item,
        generate_derivatives,
        run_ocr,
        generate_metadata,
        generate_alt_text,
        index_for_rag,
        index_for_dams_search,
    ],
    description="Run the full pipeline: ingest → derivatives → OCR → metadata → alt text → RAG + DAMS search index.",
)
