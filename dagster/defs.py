"""
Dagster definitions for the Argonath digital collections pipeline.

Submodule layout (all files live alongside this one in dagster/):
  lib/configs.py         — Config schemas and partition definitions
  lib/utils.py           — DB connection, exec() helper, manifest writer
  lib/assets.py          — All @dg.asset definitions
  lib/jobs.py            — All dg.define_asset_job definitions
  lib/sensors.py         — All @dg.sensor definitions
  lib/pipeline_fanout.py — gold_dcs_process_collection, the op-based collection-
                            level fan-out alternative to gold_dcs_derivatives's
                            per-item partitions (derivatives only; see its
                            own docstring)
  lib/pipeline_workspace.py — workspace_process, the same fan-out pattern
                            over a CaskFS workspace collection (scans staged
                            before they have an ARK; see its own docstring)
  lib/pipeline_item.py   — gold_dcs_process_item, an unpartitioned run of one item's
                            derivatives (addressed by its bronze tree folder),
                            finalize and Elasticsearch upsert, plus
                            process_item_failure
                            (see its own docstring)
  lib/pipeline_collection.py — item_queue (gold_dcs_process_item runs
                            from the queue) and gold_dcs_collection_rollup/sensor.
  lib/pipeline_index.py  — gold_dcs_reindex_collection, gold_dcs_apply_cask_changes
                            (audit reindex/delete changes),
                            gold_dcs_delete_collection_index, gold_dcs_unpublish_collection
                            and gold_dcs_delete_item_index
  lib/collection_state.py — the collection_state queue/results store, via
                            `digtk state` (see digtk.models.collection_state)

Note: Dagster inserts this file's parent directory into sys.path when loading
via python_file in workspace.yaml, so sibling-module imports work without
an __init__.py. Do not add __init__.py to this directory — it would shadow
the installed dagster package.
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
from lib.jobs import (
    bronze_reconcile_collection,
    silver_detect_spreads,
    silver_apply_spreads,
    silver_ingest_collection,
    gold_dcs_derivatives,
    silver_ocr,
    silver_correct_ocr,
    silver_metadata,
    silver_alt_text,
    silver_tag_pdf,
    silver_build_pdf,
    silver_figures,
    gold_dcs_search_index,
    silver_full_pipeline,
    preservica_import_collection_job
)
from lib.sensors import (
    bronze_preservica_auto_sync,
    bronze_collection_partition_sync,
    bronze_cask_audit,
    workspace_partition_sync,
)
from lib.pipeline_fanout import gold_dcs_process_collection
from lib.pipeline_workspace import workspace_process
from lib.pipeline_item import gold_dcs_process_item, process_item_failure
from lib.pipeline_collection import (
    gold_dcs_collection_rollup,
    item_queue,
    gold_dcs_collection_rollup_trigger,
)
from lib.pipeline_index import (
    gold_dcs_reindex_collection,
    gold_dcs_delete_collection_index,
    gold_dcs_unpublish_collection,
    gold_dcs_delete_item_index,
    gold_dcs_apply_cask_changes,
)

defs = dg.Definitions(
    assets=[
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
    ],
    jobs=[
        bronze_reconcile_collection,
        silver_detect_spreads,
        silver_apply_spreads,
        silver_ingest_collection,
        gold_dcs_derivatives,
        silver_ocr,
        silver_correct_ocr,
        silver_metadata,
        silver_alt_text,
        silver_tag_pdf,
        silver_build_pdf,
        silver_figures,
        gold_dcs_search_index,
        silver_full_pipeline,
        gold_dcs_process_collection,
        workspace_process,
        gold_dcs_process_item,
        gold_dcs_collection_rollup,
        gold_dcs_reindex_collection,
        gold_dcs_delete_collection_index,
        gold_dcs_unpublish_collection,
        gold_dcs_delete_item_index,
        gold_dcs_apply_cask_changes,
        preservica_import_collection_job
    ],
    sensors=[
        bronze_preservica_auto_sync,
        bronze_collection_partition_sync,
        bronze_cask_audit,
        workspace_partition_sync,
        process_item_failure,
        item_queue,
        gold_dcs_collection_rollup_trigger,
    ],
    resources={},
)
