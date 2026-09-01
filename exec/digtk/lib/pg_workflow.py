"""
digtk.lib.pg_workflow -- writes fin workflow-completion markers directly into
fin's own Postgres `workflow.workflow` table (schema:
fin/services/init/postgres/001-workflow.sql), instead of uploading a JSON
marker to GCS for fin to lazily bootstrap into that same table (see
digtk.lib.gcs_sync's module docstring for that path, and
fin/services/fin/node-utils/lib/gc/workflow-model.js's
workflowExistsInGcs()/loadWorkflowFromGcs()). Same practical effect -- stops
fin's createWorkflow() from re-running its Cloud Workflow derivative pipeline
for a (finPath, name) it already considers done -- via a direct write instead
of the GCS round-trip.

Named 'pg', not 'fin': argonath is moving off fin and may eventually remove
it entirely, so this module is scoped to "write directly into fin's workflow
table" for as long as that table exists, not framed as a fin-specific
abstraction other code should build on.

Connects to fin's OWN Postgres instance (database 'fcrepo', see
fin/services/fin/node-utils/config.js) -- a separate instance from
project-anduin's Postgres/pgvector that the rest of digtk (search/RAG) talks
to. See CONFIG["fin_pg"] / DIGTK_FIN_PG_* for connection settings. For
local-dev, this means a reachable host/port for dams-deployment's local-dev
`postgres` Service (e.g. via `kubectl port-forward svc/postgres 5432:5432
-n dams`) -- there is no real network path configured here for any other
environment yet.

Reuses gcs_sync._workflow_marker() for the row shape rather than re-deriving
it -- that shape took ten confirmed bug fixes to get right against a real fin
import (see gcs_sync's module docstring), and duplicating it here would risk
the same drift.
"""
import os

from digtk.lib import gcs_sync, items
from digtk.lib.config import CONFIG
from digtk.models.digital_website import pipeline

# fin's fin_workflow_state Postgres enum, verbatim
# (fin/services/init/postgres/001-workflow.sql). Locked to exactly these
# values -- extend explicitly if fin's enum ever grows, don't accept
# arbitrary state strings.
STATES = ("pending", "init", "running", "completed", "deleted", "error")

_UPSERT_SQL = """
    INSERT INTO workflow.workflow (workflow_id, created, updated, type, name, state, data, error)
    VALUES (%(workflow_id)s, %(created)s, %(updated)s, %(type)s, %(name)s, %(state)s, %(data)s, %(error)s)
    ON CONFLICT (workflow_id) DO UPDATE SET
        updated = EXCLUDED.updated,
        state = EXCLUDED.state,
        data = EXCLUDED.data,
        error = EXCLUDED.error
"""

# Per-format _fin_path() args -- mirrors gcs_sync.sync_image_item()/
# sync_pdf_item()/sync_video_item()'s own call sites exactly: an image binary
# nests under 'media/images' when wrapped, a PDF under plain 'media', and a
# video is never wrapped at all regardless of the item's other binaries (see
# gcs_sync._fin_path()'s and sync_video_item()'s docstrings for the evidence).
_FIN_PATH_ARGS = {
    "image": lambda is_media: {"media": is_media, "subcontainer": "images"},
    "pdf": lambda is_media: {"media": is_media, "subcontainer": None},
    "video": lambda is_media: {"media": False, "subcontainer": None},
}


def _connect():
    """Lazily connects to fin's own Postgres instance.

    Import kept lazy so importing digtk.lib.pg_workflow doesn't require
    psycopg2 unless a write actually runs -- matches
    gcs_sync._gcs_client()'s lazy import of google-cloud-storage.

    :returns: Open connection to fin's 'fcrepo' database.
    :rtype: psycopg2.extensions.connection
    """
    import psycopg2

    cfg = CONFIG["fin_pg"]
    return psycopg2.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["database"],
        user=cfg["user"],
        password=os.getenv(cfg["password_env"]) or None,
    )


def _upsert_row(conn, marker):
    """Upserts one workflow-completion marker as a row in fin's
    workflow.workflow table -- an INSERT ... ON CONFLICT keyed on
    workflow_id, mirroring fin's own reloadWorkflow() upsert semantics
    (fin/services/fin/node-utils/lib/workflow/postgres.js). marker's
    workflow_id is deterministic (see gcs_sync._workflow_marker()), so
    re-running this for the same (fin_path, workflow_name) always upserts
    the same row rather than accumulating duplicates.

    :param psycopg2.extensions.connection conn:
    :param dict marker: A gcs_sync._workflow_marker() dict.
    """
    from psycopg2.extras import Json

    with conn.cursor() as cur:
        cur.execute(_UPSERT_SQL, {
            "workflow_id": marker["workflow_id"],
            "created": marker["created"],
            "updated": marker["updated"],
            "type": marker["type"],
            "name": marker["name"],
            "state": marker["state"],
            "data": Json(marker["data"]),
            "error": marker["error"],
        })
    conn.commit()


def mark(ark, state, opts={}):
    """Writes a workflow-completion marker directly into fin's Postgres for
    every image/PDF/video bronze binary an item has, using the exact row
    shape gcs_sync._workflow_marker() already produces (execution-tested
    against real fin imports) -- see this module's docstring.

    Mirrors gcs_sync.sync_item()'s per-format iteration (same
    _fin_path()/_needs_media_wrapper() structural inference and
    formats-present pre-filtering, to avoid pipeline.filter_binaries_by_format()
    raising on a format the item simply doesn't have), but writes straight to
    Postgres instead of uploading files/markers to GCS. Audio is never
    marked, same as gcs_sync -- fin's AUTO_WORKFLOWS has no audio entry.

    :param str ark: Item ARK.
    :param str state: One of STATES.
    :param dict opts: 'bucket' (str, default CONFIG["gcs"]["bucket"] -- only
        used to populate the marker's data.gcsBucket/data.tmpGcsBucket
        fields, matching what fin's own workflow-model.js expects there; no
        GCS upload happens in this path).
    :returns: {"status": "success", "ark": str, "marked": {media_type: {filename: finPath}}}
    :rtype: dict
    :raises ValueError: If state is not one of STATES.
    """
    if state not in STATES:
        raise ValueError(f"Invalid state {state!r}. Must be one of: {', '.join(STATES)}")

    bucket = opts.get("bucket") or CONFIG["gcs"]["bucket"]
    item = items.resolve(ark)
    is_media = gcs_sync._needs_media_wrapper(item)

    formats_present = {
        pipeline.FORMAT_BY_EXTENSION.get(os.path.splitext(p)[1].lower())
        for p in item.bronze_binary_paths
    }

    marked = {}
    conn = _connect()
    try:
        for fmt, workflow_name in gcs_sync.WORKFLOW_NAME.items():
            if fmt not in formats_present:
                continue

            paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, fmt)
            gcs_subpath = gcs_sync.GCS_SUBPATH[fmt]
            fin_path_args = _FIN_PATH_ARGS[fmt](is_media)

            format_results = {}
            for bronze_path in paths:
                filename = bronze_path.rsplit("/", 1)[-1]
                fin_path = gcs_sync._fin_path(ark, filename, **fin_path_args)
                marker = gcs_sync._workflow_marker(
                    fin_path=fin_path, bucket=bucket, gcs_subpath=gcs_subpath,
                    workflow_name=workflow_name, state=state,
                )
                _upsert_row(conn, marker)
                format_results[filename] = fin_path

            marked[fmt] = format_results
    finally:
        conn.close()

    return {"status": "success", "ark": ark, "marked": marked}
