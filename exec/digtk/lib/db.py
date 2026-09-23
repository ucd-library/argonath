"""
Generic Postgres connection/query helper for argonath's own app-level schema
(the collection registry — see digtk.models.collection). Domain-agnostic, per
CLAUDE.md's lib-vs-models split: this module knows nothing about "collections."

A distinct database/credentials from Dagster's own run/event-log storage —
dagster/lib/utils.py connects separately, via DAGSTER_POSTGRES_* env vars, to
that one (configured by dagster/dagster.yaml's storage.postgres block). Both
happen to point at the shared Postgres/pgvector instance project-anduin
provisions (see README), but as distinct databases/credentials.

Everything argonath owns in that shared instance lives under its own 'argonath'
Postgres schema (SCHEMA below), namespaced apart from anything else there (e.g.
project-anduin's own tables, or the pgvector store digtk.lib.ai's RAG indexing
uses) — every connection's search_path is set to it, and callers should still
schema-qualify table names in their own SQL (see digtk.models.collection) rather
than rely on search_path alone.

Connection settings resolve the same way as everything else in CONFIG (built-in
default -> home config file -> environment variable — see digtk.lib.config's
module docstring); 'password' is a secret, resolved only via
DIGTK_POSTGRES_PASSWORD, never persisted in CONFIG or logged.
"""
import psycopg2
import psycopg2.extras
import os

from digtk.lib.config import CONFIG

SCHEMA = "argonath"


def connect():
    """Open a new connection to argonath's own Postgres database, scoped to
    the 'argonath' schema (SCHEMA).

    :returns: A live psycopg2 connection.
    :rtype: psycopg2.extensions.connection
    :raises ValueError: If no password is set (DIGTK_POSTGRES_PASSWORD unset).
    """
    password = os.environ.get(CONFIG["postgres"]["password_env"])
    if not password:
        raise ValueError(
            f"Postgres password not set — set {CONFIG['postgres']['password_env']} "
            "or `digtk config set postgres.password`"
        )
    return psycopg2.connect(
        host=CONFIG["postgres"]["host"],
        port=CONFIG["postgres"]["port"],
        dbname=CONFIG["postgres"]["database"],
        user=CONFIG["postgres"]["user"],
        password=password,
        options=f"-c search_path={SCHEMA},public",
    )


def query(sql, params=(), opts={}):
    """Run a SELECT and return every matching row as a dict.

    :param str sql: SQL query, with %s-style placeholders.
    :param tuple|dict params: Query parameters.
    :param dict opts: 'conn' (psycopg2 connection, injectable for tests/reuse —
        defaults to a new connect(), closed before returning).
    :returns: Matching rows, each as {column: value}.
    :rtype: list[dict]
    """
    conn = opts.get("conn") or connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]
    finally:
        if "conn" not in opts:
            conn.close()


def run_script(sql, opts={}):
    """Run a raw, multi-statement SQL script (no parameters — e.g. a schema
    bootstrap file) and commit. Unlike execute()/query(), `sql` may contain
    several ';'-separated statements; psycopg2 passes the whole string through
    to the server in one round trip.

    :param str sql: Raw SQL script.
    :param dict opts: 'conn' (psycopg2 connection, injectable for tests/reuse —
        defaults to a new connect(), closed before returning).
    """
    conn = opts.get("conn") or connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
    finally:
        if "conn" not in opts:
            conn.close()


def execute(sql, params=(), opts={}):
    """Run an INSERT/UPDATE/DELETE and commit.

    :param str sql: SQL statement, with %s-style placeholders.
    :param tuple|dict params: Statement parameters.
    :param dict opts: 'conn' (psycopg2 connection, injectable for tests/reuse —
        defaults to a new connect(), closed before returning), 'returning' (bool
        — fetch and return rows, for statements with a RETURNING clause).
    :returns: Rows from a RETURNING clause if opts['returning'], else None.
    :rtype: list[dict] | None
    """
    conn = opts.get("conn") or connect()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            result = [dict(row) for row in cur.fetchall()] if opts.get("returning") else None
        conn.commit()
        return result
    finally:
        if "conn" not in opts:
            conn.close()
