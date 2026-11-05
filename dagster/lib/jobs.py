"""
Dagster job definitions for the Argonath pipeline.

Asset jobs for the bronze phase. Op-based jobs live in the lib/pipeline_*.py modules.
"""
import dagster as dg

from lib.assets import (
    preservica_import_collection,
    reconcile_collection_items,
    rebuild_collection_tree,
)

bronze_preservica_import_collection = dg.define_asset_job(
    name="bronze_preservica_import_collection",
    selection=[preservica_import_collection],
    description="Sync a Preservica collection into bronze cask using `digtk preservica import`, "
    "then rebuild its tree structure (`digtk preservica tree`). "
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
