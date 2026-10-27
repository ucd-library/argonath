import pytest

from digtk.models.digital_website import showcase_db


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql):
        self.conn.scripts.append(sql)


class FakeConn:
    def __init__(self):
        self.scripts = []
        self.commits = 0
        self.sessions = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def set_session(self, **kwargs):
        self.sessions.append(kwargs)

    def close(self):
        self.closed = True


@pytest.fixture
def init_dir(tmp_path):
    (tmp_path / "004-b.sql").write_text("SELECT 4;")
    (tmp_path / "003-a.sql").write_text("SELECT 3;")
    (tmp_path / "notes.txt").write_text("ignored")
    return tmp_path


def test_init_db_applies_sql_files_in_filename_order(init_dir):
    conn = FakeConn()

    files = showcase_db.init_db({"init_dir": str(init_dir), "conn": conn})

    assert files == ["003-a.sql", "004-b.sql"]
    assert conn.scripts == ["SELECT 3;", "SELECT 4;"]
    assert conn.commits == 2
    assert not conn.closed


def test_init_db_uses_the_configured_dir_and_closes_its_own_connection(init_dir, monkeypatch):
    conn = FakeConn()
    monkeypatch.setitem(showcase_db.CONFIG["showcase_db"], "init_dir", str(init_dir))
    monkeypatch.setattr(showcase_db, "connect", lambda opts={}: conn)

    assert showcase_db.init_db() == ["003-a.sql", "004-b.sql"]
    assert conn.closed


def test_init_db_raises_without_sql_files(tmp_path):
    with pytest.raises(FileNotFoundError):
        showcase_db.init_db({"init_dir": str(tmp_path), "conn": FakeConn()})


def test_connect_is_writable_unless_read_only(monkeypatch):
    conns = []
    monkeypatch.setattr(showcase_db.psycopg2, "connect", lambda **kwargs: conns.append(FakeConn()) or conns[-1])

    showcase_db.connect()
    showcase_db.connect({"readonly": True})

    assert conns[0].sessions == []
    assert conns[1].sessions == [{"readonly": True}]
