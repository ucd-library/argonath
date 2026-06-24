"""
Dagster sensor definitions — chain pipeline phases on job completion.
"""
import dagster as dg


@dg.sensor(job_name="derivatives_job")
def derivatives_on_ingest(context: dg.SensorEvaluationContext):
    """Trigger derivatives_job when ingest_collection_job completes successfully.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("phase 0.4")


@dg.sensor(job_name="ocr_job")
def ocr_on_derivatives(context: dg.SensorEvaluationContext):
    """Trigger ocr_job when derivatives_job completes successfully.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("phase 0.5")


@dg.sensor(job_name="metadata_job")
def metadata_on_ocr(context: dg.SensorEvaluationContext):
    """Trigger metadata_job when ocr_job completes successfully.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("phase 0.6")


@dg.sensor(job_name="search_index_job")
def rag_on_metadata(context: dg.SensorEvaluationContext):
    """Trigger search_index_job when metadata_job completes successfully.

    :param context: Dagster sensor evaluation context.
    """
    raise NotImplementedError("phase 0.7")
