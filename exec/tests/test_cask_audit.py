from digtk.lib import cask_audit


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params):
        self.conn.calls.append((repr(statement), params))

    def fetchall(self):
        return self.conn.rows


class _Conn:
    """Records each query; answers with fixed rows."""

    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.closed = False

    def cursor(self, cursor_factory=None):
        return _Cursor(self)

    def close(self):
        self.closed = True


def test_read_events_filters_by_cursor_and_prefix():
    conn = _Conn([{"audit_log_id": 5, "resource_path": "/bronze/g/c/latest/tree/a/x.tif"}])

    rows = cask_audit.read_events(4, {"prefixes": ["/bronze/g/c/latest/tree/"], "conn": conn, "limit": 10})

    assert rows == [{"audit_log_id": 5, "resource_path": "/bronze/g/c/latest/tree/a/x.tif"}]
    statement, params = conn.calls[0]
    assert "audit_log_id > %s" in statement
    assert "fromPath" in statement
    assert "caskfs" in statement
    assert params == (4, ["/bronze/g/c/latest/tree/%"], ["/bronze/g/c/latest/tree/%"], 10)
    assert conn.closed is False


def test_read_events_escapes_like_wildcards():
    conn = _Conn([])

    cask_audit.read_events(0, {"prefixes": ["/bronze/g/my_coll/"], "conn": conn})

    assert conn.calls[0][1][1] == ["/bronze/g/my\\_coll/%"]
    assert conn.calls[0][1][3] == cask_audit.DEFAULT_LIMIT


def test_read_events_without_prefixes_queries_nothing():
    conn = _Conn([{"audit_log_id": 1}])

    assert cask_audit.read_events(0, {"prefixes": [], "conn": conn}) == []
    assert conn.calls == []


def test_max_event_id():
    assert cask_audit.max_event_id({"conn": _Conn([{"id": 42}])}) == 42
    assert cask_audit.max_event_id({"conn": _Conn([{"id": 0}])}) == 0


def test_table_uses_the_configured_schema(monkeypatch):
    monkeypatch.setitem(cask_audit.CONFIG["caskfs_db"], "schema", "other")

    assert "other" in repr(cask_audit._table())
