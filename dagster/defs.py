"""
Dagster definitions for the Argonath digital collections pipeline.

Submodule layout (all files live alongside this one in dagster/):
  lib/configs.py   — Config schemas and partition definitions
  lib/utils.py     — DB connection, exec() helper, manifest writer
  lib/assets.py    — All @dg.asset definitions
  lib/jobs.py      — All dg.define_asset_job definitions
  lib/sensors.py   — All @dg.sensor definitions
  lib/schedules.py — All dg.ScheduleDefinition instances

Note: Dagster inserts this file's parent directory into sys.path when loading
via python_file in workspace.yaml, so sibling-module imports work without
an __init__.py. Do not add __init__.py to this directory — it would shadow
the installed dagster package.
"""
import dagster as dg

from lib.assets import (
    reconcile_collection_items,
    rebuild_collection_tree,
    ingest_item,
    generate_derivatives,
    run_ocr,
    generate_metadata,
    index_for_rag,
    index_for_dams_search,
)
from lib.jobs import (
    reconcile_collection_job,
    ingest_collection_job,
    derivatives_job,
    ocr_job,
    metadata_job,
    search_index_job,
    dams_search_index_job,
    full_pipeline_job,
)
from lib.sensors import (
    preservica_auto_sync_sensor,
    collection_partition_sync_sensor,
    derivatives_on_ingest,
    ocr_on_derivatives,
    metadata_on_ocr,
    rag_on_metadata,
    dams_search_on_derivatives,
)
from lib.schedules import (
    nightly_arda_index,
)

defs = dg.Definitions(
    assets=[
        reconcile_collection_items,
        rebuild_collection_tree,
        ingest_item,
        generate_derivatives,
        run_ocr,
        generate_metadata,
        index_for_rag,
        index_for_dams_search,
    ],
    jobs=[
        reconcile_collection_job,
        ingest_collection_job,
        derivatives_job,
        ocr_job,
        metadata_job,
        search_index_job,
        dams_search_index_job,
        full_pipeline_job,
    ],
    sensors=[
        preservica_auto_sync_sensor,
        collection_partition_sync_sensor,
        derivatives_on_ingest,
        ocr_on_derivatives,
        metadata_on_ocr,
        rag_on_metadata,
        dams_search_on_derivatives,
    ],
    schedules=[
        nightly_arda_index,
    ],
    resources={},
)
