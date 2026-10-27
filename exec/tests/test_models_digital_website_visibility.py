import psycopg2
import pytest

from digtk.models.digital_website import showcase_db, visibility

PUBLIC_ID = "https://digital.ucdavis.edu/collection/ark:/87287/d7pub"
PRIVATE_ID = "https://digital.ucdavis.edu/collection/ark:/87287/d7priv"
MISSING_ID = "https://digital.ucdavis.edu/collection/ark:/87287/d7none"


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.ids = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params):
        self.conn.queries.append((statement, params))
        self.ids = params[0]

    def fetchall(self):
        return [(i, self.conn.rows[i]) for i in self.ids if i in self.conn.rows]


class _Conn:
    """Stand-in psycopg2 connection serving collection_edit rows."""

    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def cursor(self):
        return _Cursor(self)


@pytest.fixture
def conn():
    return _Conn({PUBLIC_ID: "public", PRIVATE_ID: "private"})


def test_lookup_defaults_a_missing_row_to_private(conn):
    result = visibility.lookup([PUBLIC_ID, PRIVATE_ID, MISSING_ID], {"conn": conn})
    assert result == {PUBLIC_ID: "public", PRIVATE_ID: "private", MISSING_ID: "private"}
    assert len(conn.queries) == 1


def test_lookup_treats_a_null_visibility_as_private():
    conn = _Conn({PUBLIC_ID: None})
    assert visibility.lookup([PUBLIC_ID], {"conn": conn}) == {PUBLIC_ID: "private"}


def test_lookup_skips_the_query_without_ids(conn):
    assert visibility.lookup([], {"conn": conn}) == {}
    assert conn.queries == []


def test_roles_map_visibility_to_es_roles(conn):
    assert visibility.roles(PUBLIC_ID, {"conn": conn}) == ["public", "admin"]
    assert visibility.roles(PRIVATE_ID, {"conn": conn}) == ["admin"]
    assert visibility.roles(MISSING_ID, {"conn": conn}) == ["admin"]


def test_roles_without_a_collection_is_private_and_skips_the_query(conn):
    assert visibility.roles(None, {"conn": conn}) == ["admin"]
    assert conn.queries == []


def test_roles_cache_looks_each_collection_up_once(conn):
    opts = {"conn": conn, "roles_cache": {}}
    visibility.roles(PUBLIC_ID, opts)
    roles = visibility.roles(PUBLIC_ID, opts)
    roles.append("mutated")
    assert visibility.roles(PUBLIC_ID, opts) == ["public", "admin"]
    assert len(conn.queries) == 1


def test_roles_raises_when_the_database_is_unreachable(monkeypatch):
    def fail(**kwargs):
        raise psycopg2.OperationalError("connection refused")
    monkeypatch.setattr(showcase_db.psycopg2, "connect", fail)
    with pytest.raises(psycopg2.OperationalError):
        visibility.roles(PUBLIC_ID)


def test_connect_opens_a_read_only_session(monkeypatch):
    sessions = []

    class FakeConn:
        def set_session(self, **kwargs):
            sessions.append(kwargs)
    seen = {}
    monkeypatch.setattr(showcase_db.psycopg2, "connect", lambda **kwargs: seen.update(kwargs) or FakeConn())

    visibility.connect()

    assert sessions == [{"readonly": True}]
    assert seen["dbname"] == showcase_db.CONFIG["showcase_db"]["database"]
