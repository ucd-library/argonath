import pytest

from digtk.lib import db
from digtk.lib.config import CONFIG


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.executed = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed = (sql, params)

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, rows=()):
        self.rows = rows
        self.committed = False
        self.closed = False
        self.cursor_obj = None

    def cursor(self, cursor_factory=None):
        self.cursor_obj = FakeCursor(self.rows)
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


# --- connect() ------------------------------------------------------------

def test_connect_raises_without_password(monkeypatch):
    monkeypatch.delenv(CONFIG["postgres"]["password_env"], raising=False)
    with pytest.raises(ValueError, match="password"):
        db.connect()


def test_connect_passes_resolved_config_and_password(monkeypatch):
    monkeypatch.setenv(CONFIG["postgres"]["password_env"], "secret")
    monkeypatch.setitem(CONFIG["postgres"], "host", "dbhost")
    monkeypatch.setitem(CONFIG["postgres"], "port", 5433)
    monkeypatch.setitem(CONFIG["postgres"], "database", "argonath_test")
    monkeypatch.setitem(CONFIG["postgres"], "user", "tester")

    seen = {}

    def fake_connect(**kwargs):
        seen.update(kwargs)
        return "fake-conn"

    monkeypatch.setattr(db.psycopg2, "connect", fake_connect)
    conn = db.connect()

    assert conn == "fake-conn"
    assert seen == {
        "host": "dbhost", "port": 5433, "dbname": "argonath_test",
        "user": "tester", "password": "secret",
        "options": "-c search_path=argonath,public",
    }


def test_connect_scopes_search_path_to_the_argonath_schema(monkeypatch):
    monkeypatch.setenv(CONFIG["postgres"]["password_env"], "secret")
    seen = {}
    monkeypatch.setattr(db.psycopg2, "connect", lambda **kwargs: seen.update(kwargs))
    db.connect()
    assert seen["options"] == f"-c search_path={db.SCHEMA},public"


# --- query() ----------------------------------------------------------------

def test_query_returns_rows_as_dicts():
    conn = FakeConn(rows=[{"id": 1, "name": "a"}])
    rows = db.query("SELECT * FROM x WHERE id = %s", (1,), {"conn": conn})
    assert rows == [{"id": 1, "name": "a"}]
    assert conn.cursor_obj.executed == ("SELECT * FROM x WHERE id = %s", (1,))
    assert not conn.closed


def test_query_closes_connection_when_not_injected(monkeypatch):
    conn = FakeConn(rows=[])
    monkeypatch.setattr(db, "connect", lambda: conn)
    db.query("SELECT 1")
    assert conn.closed


# --- execute() ----------------------------------------------------------------

def test_execute_commits_and_closes_by_default(monkeypatch):
    conn = FakeConn(rows=[])
    monkeypatch.setattr(db, "connect", lambda: conn)
    result = db.execute("INSERT INTO x VALUES (%s)", (1,))
    assert result is None
    assert conn.committed
    assert conn.closed


def test_execute_returns_rows_when_returning_requested():
    conn = FakeConn(rows=[{"id": 1}])
    result = db.execute(
        "INSERT INTO x VALUES (%s) RETURNING id", (1,), {"conn": conn, "returning": True}
    )
    assert result == [{"id": 1}]
    assert conn.committed
    assert not conn.closed


def test_execute_does_not_fetch_when_returning_not_requested():
    conn = FakeConn(rows=[{"id": 1}])
    result = db.execute("DELETE FROM x WHERE id = %s", (1,), {"conn": conn})
    assert result is None
    assert conn.committed


# --- run_script() -------------------------------------------------------------

def test_run_script_executes_the_whole_script_and_commits():
    conn = FakeConn()
    db.run_script("CREATE SCHEMA IF NOT EXISTS argonath; CREATE TABLE argonath.x (id int);", {"conn": conn})
    assert conn.cursor_obj.executed == (
        "CREATE SCHEMA IF NOT EXISTS argonath; CREATE TABLE argonath.x (id int);", None,
    )
    assert conn.committed
    assert not conn.closed


def test_run_script_closes_connection_when_not_injected(monkeypatch):
    conn = FakeConn()
    monkeypatch.setattr(db, "connect", lambda: conn)
    db.run_script("SELECT 1;")
    assert conn.committed
    assert conn.closed
