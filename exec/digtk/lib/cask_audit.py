"""
Reads CaskFS's audit log (the caskfs.audit_log table) straight from Postgres.
CaskFS's HTTP audit API has no "since" or path filter, so a poller reads the
table, using audit_log_id as its cursor.
"""
import os

import psycopg2
import psycopg2.extras
from psycopg2 import sql

from digtk.lib.config import CONFIG

DEFAULT_LIMIT = 5000

_COLUMNS = "audit_log_id, created, operation, resource_type, resource_path, details"


def connect():
    """Opens a connection to CaskFS's Postgres database (CONFIG["caskfs_db"]).

    :returns: A live psycopg2 connection.
    :rtype: psycopg2.extensions.connection
    """
    cfg = CONFIG["caskfs_db"]
    password = os.getenv(cfg["password_env"]) or os.getenv(cfg["fallback_password_env"]) or None
    return psycopg2.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["database"], user=cfg["user"], password=password,
    )


def _table():
    """The audit_log table, qualified with the configured schema.

    :rtype: psycopg2.sql.Composed
    """
    return sql.SQL("{}.audit_log").format(sql.Identifier(CONFIG["caskfs_db"]["schema"]))


def _query(statement, params, opts):
    """Runs a SELECT on an injected or new connection.

    :param statement: SQL to run.
    :param tuple params: Query parameters.
    :param dict opts: 'conn' (injectable connection, left open).
    :returns: Rows as dicts.
    :rtype: list[dict]
    """
    conn = opts.get("conn") or connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(statement, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        if "conn" not in opts:
            conn.close()


def read_events(after_id, opts={}):
    """Audit rows after a cursor whose path, or a move's old path, starts
    with one of the prefixes. Oldest first.

    :param int after_id: Last audit_log_id already seen.
    :param dict opts: 'prefixes' (list[str], required), 'limit' (int,
        default 5000), 'conn'.
    :returns: [{"audit_log_id", "created", "operation", "resource_type",
        "resource_path", "details"}].
    :rtype: list[dict]
    """
    prefixes = opts.get("prefixes") or []
    if not prefixes:
        return []
    patterns = [p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for p in prefixes]
    statement = sql.SQL(
        "SELECT " + _COLUMNS + " FROM {} WHERE audit_log_id > %s "
        "AND (resource_path LIKE ANY(%s) OR details->>'fromPath' LIKE ANY(%s)) "
        "ORDER BY audit_log_id LIMIT %s"
    ).format(_table())
    return _query(statement, (after_id, patterns, patterns, opts.get("limit") or DEFAULT_LIMIT), opts)


def max_event_id(opts={}):
    """The newest audit_log_id, a starting cursor that skips history.

    :param dict opts: 'conn'.
    :returns: The id, or 0 for an empty log.
    :rtype: int
    """
    rows = _query(sql.SQL("SELECT COALESCE(MAX(audit_log_id), 0) AS id FROM {}").format(_table()), (), opts)
    return int(rows[0]["id"])
