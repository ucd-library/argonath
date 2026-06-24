"""
Dagster schedule definitions for the Argonath pipeline.
"""
import dagster as dg


nightly_arda_index = dg.ScheduleDefinition(
    name="nightly_arda_index",
    cron_schedule="0 2 * * *",
    job_name="search_index_job",
    description="Re-index the arda space into RAG nightly at 02:00.",
)
