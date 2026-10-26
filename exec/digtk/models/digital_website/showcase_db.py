"""
dc-showcase's Postgres (its `dcf` database): connections, and applying
dc-showcase's own init SQL. Settings: CONFIG["showcase_db"].
"""
import os
from pathlib import Path

import psycopg2

from digtk.lib import db
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


def init_db(opts={}):
    """Applies dc-showcase's init SQL files, in filename order. dc-showcase
    owns these; they're mounted from its repo (CONFIG["showcase_db"]["init_dir"]).

    :param dict opts: 'init_dir' (str, overrides the config); 'conn'
        (injectable psycopg2 connection, left open).
    :returns: The applied file names.
    :rtype: list[str]
    :raises FileNotFoundError: If the directory has no .sql files.
    """
    init_dir = Path(opts.get("init_dir") or CONFIG["showcase_db"]["init_dir"])
    files = sorted(init_dir.glob("*.sql"))
    if not files:
        raise FileNotFoundError(f"no .sql files in {init_dir}")

    conn = opts.get("conn") or connect()
    try:
        for path in files:
            db.run_script(path.read_text(), {"conn": conn})
    finally:
        if "conn" not in opts:
            conn.close()
    return [path.name for path in files]
