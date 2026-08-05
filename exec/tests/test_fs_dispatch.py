import pytest

from digtk.lib import fs


class FakeCaskBackend:
    """Minimal in-memory stand-in for CaskBackend, used to test digtk.lib.fs's
    dispatch/validation logic without hitting the real cask.py HTTP code."""

    def __init__(self):
        self.files = {}
        self.write_calls = []
        self.link_calls = []

    def read_bytes(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write_bytes(self, path, data, opts={}):
        self.write_calls.append((path, opts))
        self.files[path] = data
        return {"filepath": path}

    def write_stream(self, path, chunks, opts={}):
        self.write_calls.append((path, opts))
        self.files[path] = b"".join(chunks)
        return {"filepath": path}

    def ls(self, path, opts={}):
        return []

    def exists(self, path):
        return path in self.files

    def delete(self, path, opts={}):
        del self.files[path]

    def read_stream(self, path):
        from contextlib import contextmanager

        @contextmanager
        def cm():
            yield iter([self.files[path]])
        return cm()

    def add_derivative_link(self, path, source_path, opts={}):
        self.link_calls.append((path, source_path, opts))
        return {"from_filepath": path, "to_filepath": source_path}


@pytest.fixture
def fake_cask(monkeypatch):
    backend = FakeCaskBackend()
    monkeypatch.setattr(fs, "_get_cask_backend", lambda: backend)
    return backend


def test_write_requires_exactly_one_of_data_or_src(tmp_path):
    with pytest.raises(ValueError):
        fs.write(str(tmp_path / "x.txt"))
    with pytest.raises(ValueError):
        fs.write(str(tmp_path / "x.txt"), data=b"a", src=str(tmp_path / "y.txt"))


def test_write_data_to_local_then_read(tmp_path):
    path = str(tmp_path / "x.txt")
    fs.write(path, data=b"hello")
    assert fs.read(path) == b"hello"


def test_write_cask_only_opts_rejected_for_local_path(tmp_path, fake_cask):
    path = str(tmp_path / "x.txt")
    with pytest.raises(ValueError, match="partition_keys"):
        fs.write(path, data=b"hi", opts={"partition_keys": ["a"]})


def test_write_data_to_cask_uses_cask_backend(fake_cask):
    result = fs.write("cask:/bronze/x.txt", data=b"hi", opts={"partition_keys": ["a"]})
    assert result == {"filepath": "/bronze/x.txt"}
    assert fake_cask.files["/bronze/x.txt"] == b"hi"
    assert fake_cask.write_calls[0][1]["partition_keys"] == ["a"]


def test_write_src_local_to_cask_streams(tmp_path, fake_cask):
    src = str(tmp_path / "src.txt")
    fs.write(src, data=b"streamed")
    fs.write("cask:/bronze/dst.txt", src=src)
    assert fake_cask.files["/bronze/dst.txt"] == b"streamed"


def test_write_src_cask_to_local_streams(tmp_path, fake_cask):
    fake_cask.files["/bronze/src.txt"] = b"from-cask"
    dst = str(tmp_path / "dst.txt")
    fs.write(dst, src="cask:/bronze/src.txt")
    assert fs.read(dst) == b"from-cask"


def test_ls_cask_only_opts_rejected_for_local_path(tmp_path):
    with pytest.raises(ValueError, match="limit"):
        fs.ls(str(tmp_path), opts={"limit": 10})


def test_exists_and_delete_local(tmp_path):
    path = str(tmp_path / "x.txt")
    assert fs.exists(path) is False
    fs.write(path, data=b"x")
    assert fs.exists(path) is True
    fs.delete(path)
    assert fs.exists(path) is False


def test_add_derivative_link_uses_cask_backend(fake_cask):
    result = fs.add_derivative_link("cask:/silver/x.jpg", "cask:/bronze/x.tiff", opts={"relation": "r"})
    assert result == {"from_filepath": "/silver/x.jpg", "to_filepath": "/bronze/x.tiff"}
    assert fake_cask.link_calls == [("/silver/x.jpg", "/bronze/x.tiff", {"relation": "r"})]


def test_add_derivative_link_rejects_local_path(tmp_path, fake_cask):
    with pytest.raises(ValueError, match="cask:/"):
        fs.add_derivative_link(str(tmp_path / "x.jpg"), "cask:/bronze/x.tiff")
    with pytest.raises(ValueError, match="cask:/"):
        fs.add_derivative_link("cask:/silver/x.jpg", str(tmp_path / "x.tiff"))


def test_get_cask_backend_raises_without_configured_connection(monkeypatch):
    from digtk.lib import auth
    monkeypatch.setattr(auth, "resolve", lambda: {"api_url": None, "token": None, "user": None})
    monkeypatch.setattr(fs, "_cask_backend", None)
    with pytest.raises(RuntimeError, match="digtk auth login"):
        fs.read("cask:/bronze/x.txt")
