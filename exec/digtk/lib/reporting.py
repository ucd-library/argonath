"""
digtk.lib.reporting -- records per-invocation outcome rows for digtk commands
that Dagster shells out to, into a `reporting` Postgres schema, so Superset
can build ETL-monitoring dashboards over it. Ported from aggie-experts'
harvest/lib/reporting (etl_reporting.command/.error), adapted for argonath's
collection-level fan-out design (see dagster/lib/pipeline_fanout.py): one
Dagster run there covers a whole collection and fans out to many items, so
each row also carries a collection key and an item ARK, not just a job id.

Lives in the shared postgres instance's default 'postgres' database (the one
cask/anduin-gateway already use -- see argonath-deployment's compose.yaml),
in its own 'reporting' schema -- deliberately not DAGSTER_POSTGRES_* (dagster/
lib/utils.py's connection), which is Dagster's own internal run/event storage
and not meant to hold application tables. Connection settings: CONFIG["reporting"]
/ DIGTK_REPORTING_PG_*.

Reporting is opt-in per command invocation: every digtk command instrumented
with track() takes a --reporting-job-id option (digtk.lib.config.ReportingJobIdOption),
and track() is a no-op whenever job_id is falsy -- a human running a command
by hand without that flag records nothing. When Dagster invokes a command, it
always passes its own run id as --reporting-job-id (see
dagster/lib/pipeline_fanout.py's exec() call sites), which is what lets a
Superset row link back to the exact Dagster run that produced it.
"""
import os
import traceback as traceback_module
from datetime import datetime, timezone

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS reporting;

CREATE TABLE IF NOT EXISTS reporting.command (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(255) NOT NULL,
    collection VARCHAR(255),
    item_id VARCHAR(255),
    stage VARCHAR(255) NOT NULL,
    options JSONB,
    status VARCHAR(20) NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS command_job_id_idx ON reporting.command (job_id);
CREATE INDEX IF NOT EXISTS command_collection_idx ON reporting.command (collection);
CREATE INDEX IF NOT EXISTS command_item_id_idx ON reporting.command (item_id);
CREATE INDEX IF NOT EXISTS command_status_idx ON reporting.command (status);
CREATE INDEX IF NOT EXISTS command_started_at_idx ON reporting.command (started_at);

CREATE TABLE IF NOT EXISTS reporting.error (
    id BIGSERIAL PRIMARY KEY,
    command_id BIGINT NOT NULL REFERENCES reporting.command(id) ON DELETE CASCADE,
    message TEXT NOT NULL,
    traceback TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS error_command_id_idx ON reporting.error (command_id);

-- One row per failed command with its error inlined -- the shape Superset's
-- "Errors" dashboard tab reads from. job_link lets a cell render as a
-- clickable deep link back into the Dagster run UI (see
-- doc/dagster-harvest-workflow.md's aggie-experts precedent for this pattern).
CREATE OR REPLACE VIEW reporting.command_error AS
SELECT
    c.id, c.job_id, c.collection, c.item_id, c.stage, c.options,
    c.status, c.started_at, c.finished_at,
    e.message AS error_message, e.traceback AS error_traceback,
    '/dagster/runs/' || c.job_id AS job_link
FROM reporting.command c
JOIN reporting.error e ON e.command_id = c.id;

-- One row per job (Dagster run) -- what Superset's "Runs" dashboard tab reads
-- from, rather than every chart re-aggregating reporting.command itself.
-- finished_at is null while any command for the job is still running, even
-- if others have already finished -- MAX() alone would otherwise report a
-- stale finish time for a run that's still in progress.
CREATE OR REPLACE VIEW reporting.run_summary AS
SELECT
    job_id,
    collection,
    MIN(started_at) AS started_at,
    CASE WHEN bool_or(status = 'running') THEN NULL ELSE MAX(finished_at) END AS finished_at,
    count(*) AS total_commands,
    count(*) FILTER (WHERE status = 'success') AS success_count,
    count(*) FILTER (WHERE status = 'failed') AS failed_count,
    count(*) FILTER (WHERE status = 'running') AS running_count,
    '/dagster/runs/' || job_id AS job_link
FROM reporting.command
GROUP BY job_id, collection;
"""

_INSERT_COMMAND_SQL = """
    INSERT INTO reporting.command (job_id, collection, item_id, stage, options, status)
    VALUES (%(job_id)s, %(collection)s, %(item_id)s, %(stage)s, %(options)s, 'running')
    RETURNING id
"""

_FINISH_COMMAND_SQL = """
    UPDATE reporting.command SET status = %(status)s, finished_at = %(finished_at)s
    WHERE id = %(command_id)s
"""

_INSERT_ERROR_SQL = """
    INSERT INTO reporting.error (command_id, message, traceback)
    VALUES (%(command_id)s, %(message)s, %(traceback)s)
"""

_DELETE_OLD_COMMANDS_SQL = """
    DELETE FROM reporting.command WHERE started_at < %(cutoff)s
"""


def _connect():
    """Connects to the reporting schema's Postgres instance.

    Import kept lazy so importing digtk.lib.reporting doesn't require
    psycopg2 unless a write actually runs -- matches
    digtk.lib.pg_workflow._connect()'s own lazy import.

    :returns: Open connection to the shared 'postgres' database.
    :rtype: psycopg2.extensions.connection
    """
    import psycopg2

    from digtk.lib.config import CONFIG

    cfg = CONFIG["reporting"]
    return psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["database"],
        user=cfg["user"],
        password=os.getenv(cfg["password_env"]) or None,
    )


def ensure_schema(conn=None):
    """Creates the reporting schema/tables/indexes/view if they don't already
    exist. Safe to call on every deploy or daemon startup -- every statement
    is idempotent (IF NOT EXISTS / CREATE OR REPLACE).

    :param conn: Optional open connection to reuse; a new one is opened and
        closed if omitted.
    """
    owns_conn = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA_SQL)
        conn.commit()
    finally:
        if owns_conn:
            conn.close()


class track:
    """Context manager that records a reporting.command row for one digtk
    invocation -- a 'running' row on entry, updated to 'success' or 'failed'
    (with a linked reporting.error row) on exit. A no-op throughout when
    job_id is falsy, so instrumented commands stay silent unless a caller
    opts in with --reporting-job-id.

    Never suppresses an exception raised inside the `with` block -- it
    records the failure, then lets the exception propagate so the command's
    own `except _ERRORS as e: utils.error(str(e))` handling still runs
    unchanged.

    :param str job_id: Job identifier (e.g. a Dagster run id), or None/"" to
        disable reporting for this invocation.
    :param str stage: Pipeline stage name, e.g. "derivatives", "ocr".
    :param str item_id: Item ARK this invocation processed, if any.
    :param str collection: Collection key this invocation belongs to, if known.
    :param dict options: JSON-serializable command options, recorded for context.
    """

    def __init__(self, job_id, stage, item_id=None, collection=None, options=None):
        self.job_id = job_id or None
        self.stage = stage
        self.item_id = item_id
        self.collection = collection
        self.options = options or {}
        self._conn = None
        self.command_id = None

    def __enter__(self):
        if not self.job_id:
            return self

        from psycopg2.extras import Json

        self._conn = _connect()
        with self._conn.cursor() as cur:
            cur.execute(_INSERT_COMMAND_SQL, {
                "job_id": self.job_id,
                "collection": self.collection,
                "item_id": self.item_id,
                "stage": self.stage,
                "options": Json(self.options),
            })
            self.command_id = cur.fetchone()[0]
        self._conn.commit()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if not self.job_id:
            return False

        try:
            with self._conn.cursor() as cur:
                cur.execute(_FINISH_COMMAND_SQL, {
                    "command_id": self.command_id,
                    "status": "failed" if exc_type else "success",
                    "finished_at": datetime.now(timezone.utc),
                })
                if exc_type:
                    cur.execute(_INSERT_ERROR_SQL, {
                        "command_id": self.command_id,
                        "message": str(exc_val),
                        "traceback": "".join(traceback_module.format_exception(exc_type, exc_val, exc_tb)),
                    })
            self._conn.commit()
        finally:
            self._conn.close()

        return False


def clean(days, opts={}):
    """Deletes reporting.command rows (and their cascaded reporting.error
    rows) older than `days`. Mirrors aggie-experts' `experts harvest
    reporting clean` -- a periodic Dagster asset/job, not part of the main
    pipeline, keeps this schema from growing unbounded.

    :param int days: Delete commands started more than this many days ago.
    :param dict opts: 'yes' (bool, default False) -- actually delete; a
        dry run (the default) only counts matching rows.
    :returns: {"status": "success", "deleted": int, "dry_run": bool}
    :rtype: dict
    """
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    yes = opts.get("yes", False)

    conn = _connect()
    try:
        with conn.cursor() as cur:
            if yes:
                cur.execute(_DELETE_OLD_COMMANDS_SQL, {"cutoff": cutoff})
                deleted = cur.rowcount
                conn.commit()
            else:
                cur.execute(
                    "SELECT count(*) FROM reporting.command WHERE started_at < %(cutoff)s",
                    {"cutoff": cutoff},
                )
                deleted = cur.fetchone()[0]
    finally:
        conn.close()

    return {"status": "success", "deleted": deleted, "dry_run": not yes}
