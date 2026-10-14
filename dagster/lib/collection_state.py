"""
Dagster's side of the collection_state store (see digtk.models.collection_state
and exec/digtk/sql/collection_state.sql): the job queue, item/collection job
results and collection rollups. Every call goes through the digtk CLI (`digtk
state ...`) via lib.utils.exec(), like every other Dagster/digtk interaction.

Recording a result is best-effort: record_item_result() logs a warning and
carries on if digtk or the database fails, so a missing schema or unreachable
Postgres never fails an item run. Everything else (enqueue, pending, ack,
rollups) raises, so sensor ticks and collection ops show the error.
"""
import datetime
import json

import dagster as dg

from lib.utils import exec

# job_name values in collection_state — the same names as the Dagster jobs.
ITEM_JOB = "process_item_job"
ROLLUP_JOB = "collection_rollup_job"

# Run tag set whenever an item result is recorded, so process_item_failure_sensor
# can tell which failed runs already have one.
RESULT_TAG = "argonath/result"


def _state(args):
    """Run a `digtk state` subcommand and return its result.

    :param list[str] args: Subcommand and arguments, e.g. ["rollups-due"].
    :returns: digtk's parsed JSON result.
    :rtype: dict
    :raises RuntimeError: If the command reports an error.
    """
    result = exec(["digtk", "state"] + args, {"check": False})
    if result.get("status") != "success":
        raise RuntimeError(f"digtk state {args[0]} failed: {result.get('error') or result}")
    return result


def item_result(context, item, status, opts={}):
    """Build an item job result for record_item_result().

    :param context: Dagster op or sensor context (for the run id).
    :param dict item: The item job's item dict ({"item", ...}, plus "ark" once
        pdc_generate_item_record has built the record).
    :param str status: "success" or "failed".
    :param dict opts: 'error' (str), 'run_id' (defaults to context.run_id).
    :returns: {"status", "run_id", "ark", "error", "finished_at"}.
    :rtype: dict
    """
    return {
        "status": status,
        "run_id": opts.get("run_id") or context.run_id,
        "ark": item.get("ark"),
        "error": opts.get("error"),
        "finished_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


def record_item_result(item, result, opts={}):
    """Record an item job's result in collection_state. Never raises.

    :param str item: Item bronze folder path.
    :param dict result: item_result()'s dict.
    :param dict opts: 'log' (logger, default dagster's), 'context' (op context —
        when given, the run is tagged RESULT_TAG=<status>).
    :returns: True if the result was recorded.
    :rtype: bool
    """
    log = opts.get("log") or dg.get_dagster_logger()
    cmd = ["result", "--job-name", ITEM_JOB, "--item", item, "--result", json.dumps(result)]
    if result.get("ark"):
        cmd += ["--ark", result["ark"]]
    context = opts.get("context")
    if context is not None:
        context.instance.add_run_tags(context.run_id, {RESULT_TAG: result["status"]})
    try:
        _state(cmd)
    except Exception as e:
        log.warning(f"collection_state: couldn't record {result['status']} for {item}: {e}")
        return False
    return True


def enqueue_collection(key, opts={}):
    """Queue item runs for a collection (`digtk state enqueue-collection`).

    :param str key: Collection key, e.g. "bronze/preservica/haeger".
    :param dict opts: 'batch' (str, e.g. the launching run id).
    :returns: {"collection", "items", "added", "removed", "enqueued"}.
    :rtype: dict
    """
    args = ["enqueue-collection", "--collection-key", key]
    if opts.get("batch"):
        args += ["--batch", opts["batch"]]
    return _state(args)


def pending(job_name, limit):
    """The oldest queued rows for a job, without removing them.

    :param str job_name: e.g. ITEM_JOB.
    :param int limit: Most rows to return.
    :returns: [{"id", "config", "item", "collection", ...}].
    :rtype: list[dict]
    """
    return _state(["pending", "--job-name", job_name, "--limit", str(limit)])["rows"]


def ack(ids):
    """Remove queued rows whose runs were submitted.

    :param list[int] ids: job_queue ids.
    :returns: How many rows were removed.
    :rtype: int
    """
    if not ids:
        return 0
    args = ["ack"]
    for i in ids:
        args += ["--id", str(i)]
    return _state(args)["removed"]


def rollups_due():
    """Collections whose items finished since their last rollup.

    :returns: [{"collection", "latest_result_at"}].
    :rtype: list[dict]
    """
    return _state(["rollups-due"])["collections"]


def rollup(key):
    """Recompute and record a collection's rollup.

    :param str key: Collection key.
    :returns: {"collection", "item_count", "succeeded", "failed", "never_run", "failed_items", "rolled_up_at"}.
    :rtype: dict
    """
    return _state(["rollup", "--collection-key", key])
