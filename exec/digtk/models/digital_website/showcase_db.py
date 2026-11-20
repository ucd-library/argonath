"""
Connections to dc-showcase's Postgres (its `dcf` database). dc-showcase owns
the schema (its client's bin/init-db.js). Settings: CONFIG["showcase_db"].
"""
import os

import psycopg2

from digtk.lib.config import CONFIG


def connect(opts={}):
    """Opens a connection to dc-showcase's Postgres (CONFIG["showcase_db"]).

    :param dict opts: 'readonly' (bool, default False).
    :returns: A live psycopg2 connection.
    :rtype: psycopg2.extensions.connection
    """
    cfg = CONFIG["showcase_db"]
    conn = psycopg2.connect(
        host=cfg["host"], port=cfg["port"], dbname=cfg["database"], user=cfg["user"],
        password=os.getenv(cfg["password_env"]) or None,
    )
    if opts.get("readonly"):
        conn.set_session(readonly=True)
    return conn

