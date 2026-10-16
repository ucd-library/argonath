"""
Dagster definitions for the Argonath digital collections pipeline.

Submodule layout (all files live alongside this one in dagster/):
  lib/configs.py         — Config schemas and partition definitions
  lib/utils.py           — DB connection, exec() helper, manifest writer
  lib/assets.py          — All @dg.asset definitions
  lib/jobs.py            — All dg.define_asset_job definitions
  lib/sensors.py         — All @dg.sensor definitions
  lib/schedules.py       — All dg.ScheduleDefinition instances
  lib/pipeline_fanout.py — process_collection_job, the op-based collection-
                            level fan-out alternative to derivatives_job's
                            per-item partitions (derivatives only; see its
                            own docstring)
  lib/pipeline_workspace.py — process_workspace_job, the same fan-out pattern
                            over a CaskFS workspace collection (scans staged
                            before they have an ARK), and promote_workspace_job,
                            which copies reviewed items into silver (see its
                            own docstring)
  lib/pipeline_item.py   — process_item_job, an unpartitioned run of one item's
                            derivatives (addressed by its bronze tree folder),
                            finalize and Elasticsearch upsert, plus
                            process_item_failure_sensor
                            (see its own docstring)
  lib/pipeline_collection.py — collection_launch_job (queues one item run per
                            item of a collection), item_queue_sensor (launches
                            them with backpressure), collection_rollup_job and
                            collection_rollup_sensor (item count + failed items
                            once a collection's runs finish)
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
    generate_metadata,
    generate_alt_text,
    tag_accessible_pdf,
    index_for_rag,
    index_for_showcase_search,
)
from lib.jobs import (
    reconcile_collection_job,
    detect_spreads_job,
    apply_spreads_job,
    ingest_collection_job,
    derivatives_job,
    ocr_job,
    metadata_job,
    alt_text_job,
    tag_pdf_job,
    search_index_job,
    showcase_search_index_job,
    full_pipeline_job,
)
from lib.sensors import (
    preservica_auto_sync_sensor,
    collection_partition_sync_sensor,
    workspace_partition_sync_sensor,
    derivatives_on_ingest,
    ocr_on_derivatives,
    metadata_on_ocr,
    rag_on_metadata,
    showcase_search_on_derivatives,
)
from lib.schedules import (
    nightly_arda_index,
)
from lib.pipeline_fanout import process_collection_job
from lib.pipeline_workspace import process_workspace_job, promote_workspace_job
from lib.pipeline_item import process_item_job, process_item_failure_sensor
from lib.pipeline_collection import (
    collection_launch_job,
    collection_rollup_job,
    item_queue_sensor,
    collection_rollup_sensor,
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
        generate_metadata,
        generate_alt_text,
        tag_accessible_pdf,
        index_for_rag,
        index_for_showcase_search,
    ],
    jobs=[
        reconcile_collection_job,
        detect_spreads_job,
        apply_spreads_job,
        ingest_collection_job,
        derivatives_job,
        ocr_job,
        metadata_job,
        alt_text_job,
        tag_pdf_job,
        search_index_job,
        showcase_search_index_job,
        full_pipeline_job,
        process_collection_job,
        process_workspace_job,
        promote_workspace_job,
        process_item_job,
        collection_launch_job,
        collection_rollup_job,
    ],
    sensors=[
        preservica_auto_sync_sensor,
        collection_partition_sync_sensor,
        workspace_partition_sync_sensor,
        derivatives_on_ingest,
        ocr_on_derivatives,
        metadata_on_ocr,
        rag_on_metadata,
        showcase_search_on_derivatives,
        process_item_failure_sensor,
        item_queue_sensor,
        collection_rollup_sensor,
    ],
    schedules=[
        nightly_arda_index,
    ],
    resources={},
)
