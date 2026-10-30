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
    correct_ocr,
    generate_metadata,
    generate_alt_text,
    tag_accessible_pdf,
    build_accessible_pdf,
    detect_figures,
    index_for_rag,
    index_for_showcase_search,
)

reconcile_collection_job = dg.define_asset_job(
    name="reconcile_collection_job",
    selection=[reconcile_collection_items, rebuild_collection_tree],
    description="Reconcile a collection's bronze item list against its source, register/prune "
    "partitions, and (Preservica sources) rebuild its tree/ layout. bronze_cask_audit_sensor updates ES from the changes.",
)

bronze_detect_spreads_job = dg.define_asset_job(
    name="bronze_detect_spreads_job",
    selection=[detect_spreads],
    description="Detect the binding gutter in an item's image binaries for all partitioned items — "
                "only meaningful for items with scanned two-page spreads, so not part of full_pipeline_job.",
)

bronze_apply_spreads_job = dg.define_asset_job(
    name="bronze_apply_spreads_job",
    selection=[apply_spreads],
    description="Cut an item's confirmed spreads per its (human-reviewed) gutters.json for all "
                "partitioned items — not auto-chained from bronze_detect_spreads_job; run after review.",
)

ingest_collection_job = dg.define_asset_job(
    name="ingest_collection_job",
    selection=[ingest_item],
    description="Ingest all items in a collection (batch mode via digtk ingest batch).",
)

showcase_derivatives_job = dg.define_asset_job(
    name="showcase_derivatives_job",
    selection=[generate_derivatives],
    description="Generate image derivatives for all partitioned items.",
)

bronze_ocr_job = dg.define_asset_job(
    name="bronze_ocr_job",
    selection=[run_ocr],
    description="Run OCR for all partitioned items.",
)

bronze_correct_ocr_job = dg.define_asset_job(
    name="bronze_correct_ocr_job",
    selection=[correct_ocr],
    description="Correct OCR words from the vision transcript for all partitioned items "
                "(needs bronze_ocr_job output; makes no OCR or vision calls).",
)

metadata_job = dg.define_asset_job(
    name="metadata_job",
    selection=[generate_metadata],
    description="Generate AI metadata for all partitioned items.",
)

bronze_alt_text_job = dg.define_asset_job(
    name="bronze_alt_text_job",
    selection=[generate_alt_text],
    description="Generate AI alt text for all partitioned items.",
)

bronze_tag_pdf_job = dg.define_asset_job(
    name="bronze_tag_pdf_job",
    selection=[tag_accessible_pdf],
    description="Assemble a searchable, PDF/UA-1-tagged PDF for all partitioned "
                "items — only meaningful for PDF items, so not part of full_pipeline_job.",
)

bronze_build_pdf_job = dg.define_asset_job(
    name="bronze_build_pdf_job",
    selection=[build_accessible_pdf],
    description="Build each partitioned item's page images into one searchable, "
                "PDF/UA-1-tagged PDF — only meaningful for image items, so not part of full_pipeline_job.",
)

bronze_figures_job = dg.define_asset_job(
    name="bronze_figures_job",
    selection=[detect_figures],
    description="Detect and describe in-page figures for all partitioned items, for review "
                "in figure-review.csv before tagging with figures on.",
)

search_index_job = dg.define_asset_job(
    name="search_index_job",
    selection=[index_for_rag],
    description="Index all partitioned items into the RAG store.",
)

showcase_search_index_job = dg.define_asset_job(
    name="showcase_search_index_job",
    selection=[index_for_showcase_search],
    description="Index all partitioned items into the showcase website's Elasticsearch search index.",
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
        index_for_showcase_search,
    ],
    description="Run the full pipeline: ingest → derivatives → OCR → metadata → alt text → RAG + showcase search index.",
)
