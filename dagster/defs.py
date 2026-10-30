"""
Dagster definitions for the Argonath digital collections pipeline.

Submodule layout (all files live alongside this one in dagster/):
  lib/configs.py         — Config schemas and partition definitions
  lib/utils.py           — DB connection, exec() helper, manifest writer
  lib/assets.py          — All @dg.asset definitions
  lib/jobs.py            — All dg.define_asset_job definitions
  lib/sensors.py         — All @dg.sensor definitions
  lib/schedules.py       — All dg.ScheduleDefinition instances
  lib/pipeline_fanout.py — showcase_process_collection_job, the op-based collection-
                            level fan-out alternative to showcase_derivatives_job's
                            per-item partitions (derivatives only; see its
                            own docstring)
  lib/pipeline_workspace.py — workspace_process_job, the same fan-out pattern
                            over a CaskFS workspace collection (scans staged
                            before they have an ARK), and workspace_promote_job,
                            which copies reviewed items into silver (see its
                            own docstring)
  lib/pipeline_item.py   — showcase_process_item_job, an unpartitioned run of one item's
                            derivatives (addressed by its bronze tree folder),
                            finalize and Elasticsearch upsert, plus
                            process_item_failure_sensor
                            (see its own docstring)
  lib/pipeline_collection.py — item_queue_sensor (showcase_process_item_job runs
                            from the queue) and showcase_collection_rollup_job/sensor.
  lib/pipeline_index.py  — showcase_reindex_collection_job, showcase_apply_cask_changes_job
                            (audit reindex/delete changes),
                            showcase_delete_collection_index_job and showcase_delete_item_index_job
  lib/collection_state.py — the collection_state queue/results store, via
                            `digtk state` (see digtk.models.collection_state)

Note: Dagster inserts this file's parent directory into sys.path when loading
via python_file in workspace.yaml, so sibling-module imports work without
an __init__.py. Do not add __init__.py to this directory — it would shadow
the installed dagster package.
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
from lib.jobs import (
    reconcile_collection_job,
    bronze_detect_spreads_job,
    bronze_apply_spreads_job,
    ingest_collection_job,
    showcase_derivatives_job,
    bronze_ocr_job,
    bronze_correct_ocr_job,
    metadata_job,
    bronze_alt_text_job,
    bronze_tag_pdf_job,
    bronze_build_pdf_job,
    bronze_figures_job,
    search_index_job,
    showcase_search_index_job,
    full_pipeline_job,
)
from lib.sensors import (
    preservica_auto_sync_sensor,
    collection_partition_sync_sensor,
    bronze_cask_audit_sensor,
    workspace_partition_sync_sensor,
)
from lib.schedules import (
    nightly_arda_index,
)
from lib.pipeline_fanout import showcase_process_collection_job
from lib.pipeline_workspace import workspace_process_job, workspace_promote_job
from lib.pipeline_item import showcase_process_item_job, process_item_failure_sensor
from lib.pipeline_collection import (
    showcase_collection_rollup_job,
    item_queue_sensor,
    collection_rollup_sensor,
)
from lib.pipeline_index import (
    showcase_reindex_collection_job,
    showcase_delete_collection_index_job,
    showcase_delete_item_index_job,
    showcase_apply_cask_changes_job,
)

defs = dg.Definitions(
    assets=[
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
    ],
    jobs=[
        reconcile_collection_job,
        bronze_detect_spreads_job,
        bronze_apply_spreads_job,
        ingest_collection_job,
        showcase_derivatives_job,
        bronze_ocr_job,
        bronze_correct_ocr_job,
        metadata_job,
        bronze_alt_text_job,
        bronze_tag_pdf_job,
        bronze_build_pdf_job,
        bronze_figures_job,
        search_index_job,
        showcase_search_index_job,
        full_pipeline_job,
        showcase_process_collection_job,
        workspace_process_job,
        workspace_promote_job,
        showcase_process_item_job,
        showcase_collection_rollup_job,
        showcase_reindex_collection_job,
        showcase_delete_collection_index_job,
        showcase_delete_item_index_job,
        showcase_apply_cask_changes_job,
    ],
    sensors=[
        preservica_auto_sync_sensor,
        collection_partition_sync_sensor,
        bronze_cask_audit_sensor,
        workspace_partition_sync_sensor,
        process_item_failure_sensor,
        item_queue_sensor,
        collection_rollup_sensor,
    ],
    schedules=[
        nightly_arda_index,
    ],
    resources={},
)
