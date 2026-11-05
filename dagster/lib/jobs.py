"""
Dagster job definitions for the Argonath pipeline.

Jobs group assets for per-phase and full-pipeline runs.
"""
import dagster as dg

from lib.assets import (
    preservica_import_collection,
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
    index_for_showcase_search,
)

preservica_import_collection_job = dg.define_asset_job(
    name="preservica_import_collection_job",
    selection=[preservica_import_collection],
    description="One-time full walk of a brand-new Preservica collection into bronze "
    "(`digtk preservica import`) — collection is manual input, not yet a registered partition.",
)

bronze_reconcile_collection = dg.define_asset_job(
    name="bronze_reconcile_collection",
    selection=[reconcile_collection_items, rebuild_collection_tree],
    description="Reconcile a collection's bronze item list against its source, register/prune "
    "partitions, and (Preservica sources) rebuild its tree/ layout. bronze_cask_audit updates ES from the changes.",
)

silver_detect_spreads = dg.define_asset_job(
    name="silver_detect_spreads",
    selection=[detect_spreads],
    description="Detect the binding gutter in an item's image binaries for all partitioned items — "
                "only meaningful for items with scanned two-page spreads, so not part of silver_full_pipeline.",
)

silver_apply_spreads = dg.define_asset_job(
    name="silver_apply_spreads",
    selection=[apply_spreads],
    description="Cut an item's confirmed spreads per its (human-reviewed) gutters.json for all "
                "partitioned items — not auto-chained from silver_detect_spreads; run after review.",
)

silver_ingest_collection = dg.define_asset_job(
    name="silver_ingest_collection",
    selection=[ingest_item],
    description="Ingest all items in a collection (batch mode via digtk ingest batch).",
)

gold_dcs_derivatives = dg.define_asset_job(
    name="gold_dcs_derivatives",
    selection=[generate_derivatives],
    description="Generate image derivatives for all partitioned items.",
)

silver_ocr = dg.define_asset_job(
    name="silver_ocr",
    selection=[run_ocr],
    description="Run OCR for all partitioned items.",
)

silver_correct_ocr = dg.define_asset_job(
    name="silver_correct_ocr",
    selection=[correct_ocr],
    description="Correct OCR words from the vision transcript for all partitioned items "
                "(needs silver_ocr output; makes no OCR or vision calls).",
)

silver_metadata = dg.define_asset_job(
    name="silver_metadata",
    selection=[generate_metadata],
    description="Generate AI metadata for all partitioned items.",
)

silver_alt_text = dg.define_asset_job(
    name="silver_alt_text",
    selection=[generate_alt_text],
    description="Generate AI alt text for all partitioned items.",
)

silver_tag_pdf = dg.define_asset_job(
    name="silver_tag_pdf",
    selection=[tag_accessible_pdf],
    description="Assemble a searchable, PDF/UA-1-tagged PDF for all partitioned "
                "items — only meaningful for PDF items, so not part of silver_full_pipeline.",
)

silver_build_pdf = dg.define_asset_job(
    name="silver_build_pdf",
    selection=[build_accessible_pdf],
    description="Build each partitioned item's page images into one searchable, "
                "PDF/UA-1-tagged PDF — only meaningful for image items, so not part of silver_full_pipeline.",
)

silver_figures = dg.define_asset_job(
    name="silver_figures",
    selection=[detect_figures],
    description="Detect and describe in-page figures for all partitioned items, for review "
                "in figure-review.csv before tagging with figures on.",
)

gold_dcs_search_index = dg.define_asset_job(
    name="gold_dcs_search_index",
    selection=[index_for_showcase_search],
    description="Index all partitioned items into the showcase website's Elasticsearch search index.",
)

silver_full_pipeline = dg.define_asset_job(
    name="silver_full_pipeline",
    selection=[
        ingest_item,
        generate_derivatives,
        run_ocr,
        generate_metadata,
        generate_alt_text,
        index_for_showcase_search,
    ],
    description="Run the full pipeline: ingest → derivatives → OCR → metadata → alt text → showcase search index.",
)
