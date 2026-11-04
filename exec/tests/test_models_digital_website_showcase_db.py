from digtk.models.digital_website import showcase_db


class FakeConn:
    def __init__(self):
        self.sessions = []

    def set_session(self, **kwargs):
        self.sessions.append(kwargs)


def test_connect_is_writable_unless_read_only(monkeypatch):
    conns = []
    monkeypatch.setattr(showcase_db.psycopg2, "connect", lambda **kwargs: conns.append(FakeConn()) or conns[-1])

    showcase_db.connect()
    showcase_db.connect({"readonly": True})

    assert conns[0].sessions == []
    assert conns[1].sessions == [{"readonly": True}]
