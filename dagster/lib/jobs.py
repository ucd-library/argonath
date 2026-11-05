"""
Dagster job definitions for the Argonath pipeline.

Jobs group assets for per-phase runs.
"""
import dagster as dg

from lib.assets import (
    preservica_import_collection,
    reconcile_collection_items,
    detect_spreads,
    apply_spreads,
    rebuild_collection_tree,
    run_ocr,
    correct_ocr,
    generate_metadata,
    generate_alt_text,
    tag_accessible_pdf,
    build_accessible_pdf,
    detect_figures,
)

bronze_preservica_import_collection = dg.define_asset_job(
    name="bronze_preservica_import_collection",
    selection=[preservica_import_collection],
    description="Sync a Preservica collection into bronze cask using `digtk preservica import`. "
    "Collection is a property in the launchpad vs using a registered partition. "
    "If the collection is already in bronze cask, any items that are already in bronze cask will be skipped and not removed. ",
)

bronze_reconcile_collection = dg.define_asset_job(
    name="bronze_reconcile_collection",
    selection=[reconcile_collection_items, rebuild_collection_tree],
    description="Called by `bronze_preservica_auto_sync` when preservica changes are detected. "
    "Adds new items into bronze cask for a collection, removes items that are no longer in the collection, "
    "and rebuilds the tree structure for the collection in bronze cask.",
)

silver_detect_spreads = dg.define_asset_job(
    name="silver_detect_spreads",
    selection=[detect_spreads],
    description="Detect the binding gutter in an item's image binaries for all partitioned items — "
                "only meaningful for items with scanned two-page spreads.",
)

silver_apply_spreads = dg.define_asset_job(
    name="silver_apply_spreads",
    selection=[apply_spreads],
    description="Cut an item's confirmed spreads per its (human-reviewed) gutters.json for all "
                "partitioned items — not auto-chained from silver_detect_spreads; run after review.",
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
                "items — only meaningful for PDF items.",
)

silver_build_pdf = dg.define_asset_job(
    name="silver_build_pdf",
    selection=[build_accessible_pdf],
    description="Build each partitioned item's page images into one searchable, "
                "PDF/UA-1-tagged PDF — only meaningful for image items.",
)

silver_figures = dg.define_asset_job(
    name="silver_figures",
    selection=[detect_figures],
    description="Detect and describe in-page figures for all partitioned items, for review "
                "in figure-review.csv before tagging with figures on.",
)
