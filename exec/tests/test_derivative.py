import pytest

from digtk.lib import derivative
from digtk.lib import fs


@pytest.fixture
def fake_fs(monkeypatch):
    """Stub out digtk.lib.fs so derivative.py's logic is tested without a real CaskFS."""
    state = {
        "existing": set(),
        "write_calls": [],
        "link_calls": [],
    }

    def fake_exists(path):
        return path in state["existing"]

    def fake_write(path, data=None, src=None, opts={}):
        state["write_calls"].append({"path": path, "data": data, "opts": opts})
        return {"filepath": path}

    def fake_add_derivative_link(path, source_path, opts={}):
        state["link_calls"].append((path, source_path, opts))
        return {"from_filepath": path, "to_filepath": source_path}

    monkeypatch.setattr(fs, "exists", fake_exists)
    monkeypatch.setattr(fs, "write", fake_write)
    monkeypatch.setattr(fs, "add_derivative_link", fake_add_derivative_link)
    return state


# --- dest_path -------------------------------------------------------------

def test_dest_path_silver_defaults_time_to_latest():
    path = derivative.dest_path(
        type="silver", collection="ucd-photos", rel_path="item-1/derivatives/thumb-256.jpg",
        opts={"group": "digital-collections"},
    )
    assert path == "/silver/digital-collections/ucd-photos/latest/item-1/derivatives/thumb-256.jpg"


def test_dest_path_silver_with_explicit_time():
    path = derivative.dest_path(
        type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg",
        opts={"group": "digital-collections", "time": "2026-07-22"},
    )
    assert path == "/silver/digital-collections/ucd-photos/2026-07-22/item-1/thumb.jpg"


def test_dest_path_silver_requires_group():
    with pytest.raises(ValueError, match="group"):
        derivative.dest_path(type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg", opts={})


def test_dest_path_silver_rejects_app():
    with pytest.raises(ValueError, match="app"):
        derivative.dest_path(
            type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg",
            opts={"group": "digital-collections", "app": "arda"},
        )


def test_dest_path_gold_has_no_time_dimension():
    path = derivative.dest_path(
        type="gold", collection="ucd-photos", rel_path="item-1/thumb.jpg", opts={"app": "arda"}
    )
    assert path == "/gold/arda/ucd-photos/item-1/thumb.jpg"


def test_dest_path_gold_requires_app():
    with pytest.raises(ValueError, match="app"):
        derivative.dest_path(type="gold", collection="ucd-photos", rel_path="item-1/thumb.jpg", opts={})


def test_dest_path_gold_rejects_time():
    with pytest.raises(ValueError, match="time"):
        derivative.dest_path(
            type="gold", collection="ucd-photos", rel_path="item-1/thumb.jpg",
            opts={"app": "arda", "time": "2026-07-22"},
        )


def test_dest_path_gold_rejects_group():
    with pytest.raises(ValueError, match="group"):
        derivative.dest_path(
            type="gold", collection="ucd-photos", rel_path="item-1/thumb.jpg",
            opts={"app": "arda", "group": "digital-collections"},
        )


def test_dest_path_workspace_has_no_time_dimension():
    path = derivative.dest_path(
        type="workspace", collection="my-collection", rel_path="item-1/thumb.jpg",
        opts={"group": "jrmerz"},
    )
    assert path == "/workspace/jrmerz/my-collection/item-1/thumb.jpg"


def test_dest_path_workspace_requires_group():
    with pytest.raises(ValueError, match="group"):
        derivative.dest_path(type="workspace", collection="my-collection", rel_path="item-1/thumb.jpg", opts={})


def test_dest_path_workspace_rejects_time():
    with pytest.raises(ValueError, match="time"):
        derivative.dest_path(
            type="workspace", collection="my-collection", rel_path="item-1/thumb.jpg",
            opts={"group": "jrmerz", "time": "2026-07-22"},
        )


def test_dest_path_workspace_rejects_app():
    with pytest.raises(ValueError, match="app"):
        derivative.dest_path(
            type="workspace", collection="my-collection", rel_path="item-1/thumb.jpg",
            opts={"group": "jrmerz", "app": "arda"},
        )


def test_dest_path_rejects_unknown_type():
    with pytest.raises(ValueError, match="type"):
        derivative.dest_path(type="bronze", collection="ucd-photos", rel_path="item-1/thumb.jpg", opts={})


def test_dest_path_requires_collection():
    with pytest.raises(ValueError, match="collection"):
        derivative.dest_path(
            type="silver", collection="", rel_path="item-1/thumb.jpg",
            opts={"group": "digital-collections"},
        )


def test_dest_path_requires_rel_path():
    with pytest.raises(ValueError, match="rel_path"):
        derivative.dest_path(
            type="silver", collection="ucd-photos", rel_path="", opts={"group": "digital-collections"}
        )


def test_dest_path_rejects_dot_dot_segments():
    with pytest.raises(ValueError, match="rel_path"):
        derivative.dest_path(
            type="silver", collection="ucd-photos", rel_path="../escape/thumb.jpg",
            opts={"group": "digital-collections"},
        )


def test_dest_path_strips_slashes_in_rel_path():
    path = derivative.dest_path(
        type="silver", collection="ucd-photos", rel_path="/item-1/thumb.jpg/",
        opts={"group": "digital-collections"},
    )
    assert path == "/silver/digital-collections/ucd-photos/latest/item-1/thumb.jpg"


# --- write_cask -------------------------------------------------------------

def test_write_cask_requires_data(fake_fs):
    with pytest.raises(ValueError, match="data"):
        derivative.write_cask(
            type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg", data=None,
            source_files=["cask:/bronze/x.tiff"], opts={"group": "digital-collections"},
        )


def test_write_cask_requires_non_empty_source_files(fake_fs):
    with pytest.raises(ValueError, match="source_files"):
        derivative.write_cask(
            type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg", data=b"bytes",
            source_files=[], opts={"group": "digital-collections"},
        )


def test_write_cask_rejects_non_cask_source_files(fake_fs):
    with pytest.raises(ValueError, match="cask:/"):
        derivative.write_cask(
            type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg", data=b"bytes",
            source_files=["/bronze/x.tiff"], opts={"group": "digital-collections"},
        )


def test_write_cask_rejects_self_referential_source(fake_fs):
    dest = "cask:/silver/digital-collections/ucd-photos/latest/item-1/thumb.jpg"
    fake_fs["existing"].add(dest)
    with pytest.raises(ValueError, match="itself"):
        derivative.write_cask(
            type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg", data=b"bytes",
            source_files=[dest], opts={"group": "digital-collections"},
        )


def test_write_cask_raises_file_not_found_for_missing_source(fake_fs):
    with pytest.raises(FileNotFoundError):
        derivative.write_cask(
            type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg", data=b"bytes",
            source_files=["cask:/bronze/x.tiff"], opts={"group": "digital-collections"},
        )
    assert fake_fs["write_calls"] == []
    assert fake_fs["link_calls"] == []


def test_write_cask_happy_path_writes_and_links_each_source(fake_fs):
    fake_fs["existing"].update({"cask:/bronze/a.tiff", "cask:/bronze/b.tiff"})

    result = derivative.write_cask(
        type="silver", collection="ucd-photos", rel_path="item-1/derivatives/thumb-256.jpg",
        data=b"jpegbytes", source_files=["cask:/bronze/a.tiff", "cask:/bronze/b.tiff"],
        opts={"group": "digital-collections"},
    )

    dest = "cask:/silver/digital-collections/ucd-photos/latest/item-1/derivatives/thumb-256.jpg"
    assert result["path"] == dest
    assert fake_fs["write_calls"] == [{"path": dest, "data": b"jpegbytes", "opts": {"replace": False}}]
    assert fake_fs["link_calls"] == [
        (dest, "cask:/bronze/a.tiff", {}),
        (dest, "cask:/bronze/b.tiff", {}),
    ]
    assert result["links"] == [
        {"from_filepath": dest, "to_filepath": "cask:/bronze/a.tiff"},
        {"from_filepath": dest, "to_filepath": "cask:/bronze/b.tiff"},
    ]


def test_write_cask_gold_path_has_no_time_segment(fake_fs):
    fake_fs["existing"].add("cask:/silver/digital-collections/ucd-photos/latest/item-1/thumb.jpg")

    result = derivative.write_cask(
        type="gold", collection="ucd-photos", rel_path="item-1/thumb.jpg", data=b"bytes",
        source_files=["cask:/silver/digital-collections/ucd-photos/latest/item-1/thumb.jpg"],
        opts={"app": "arda"},
    )

    assert result["path"] == "cask:/gold/arda/ucd-photos/item-1/thumb.jpg"


def test_write_cask_workspace_path_has_no_time_segment(fake_fs):
    fake_fs["existing"].add("cask:/bronze/a.tiff")

    result = derivative.write_cask(
        type="workspace", collection="my-collection", rel_path="item-1/thumb.jpg", data=b"bytes",
        source_files=["cask:/bronze/a.tiff"], opts={"group": "jrmerz"},
    )

    assert result["path"] == "cask:/workspace/jrmerz/my-collection/item-1/thumb.jpg"


def test_write_cask_passes_through_mime_type_and_replace(fake_fs):
    fake_fs["existing"].add("cask:/bronze/a.tiff")

    derivative.write_cask(
        type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg", data=b"bytes",
        source_files=["cask:/bronze/a.tiff"],
        opts={"group": "digital-collections", "mime_type": "image/jpeg", "replace": True},
    )

    assert fake_fs["write_calls"][0]["opts"] == {"replace": True, "mime_type": "image/jpeg"}


# --- write_local -------------------------------------------------------------

def test_write_local_writes_bytes_to_disk(tmp_path):
    path = str(tmp_path / "item-1" / "thumb.jpg")
    derivative.write_local(path=path, data=b"jpegbytes")
    assert fs.read(path) == b"jpegbytes"


def test_write_local_overwrites_existing_file(tmp_path):
    path = str(tmp_path / "thumb.jpg")
    derivative.write_local(path=path, data=b"first")
    derivative.write_local(path=path, data=b"second")
    assert fs.read(path) == b"second"


def test_write_local_rejects_cask_path():
    with pytest.raises(ValueError, match="cask:/"):
        derivative.write_local(path="cask:/silver/x.jpg", data=b"bytes")


# --- write (dispatcher) ------------------------------------------------------

def test_write_dispatches_to_cask_branch(fake_fs):
    fake_fs["existing"].add("cask:/bronze/a.tiff")

    result = derivative.write(
        type="silver", collection="ucd-photos", rel_path="item-1/thumb.jpg", data=b"bytes",
        source_files=["cask:/bronze/a.tiff"], opts={"group": "digital-collections"},
    )

    dest = "cask:/silver/digital-collections/ucd-photos/latest/item-1/thumb.jpg"
    assert result["path"] == dest
    assert fake_fs["write_calls"][0]["path"] == dest


def test_write_dispatches_to_local_branch(tmp_path):
    path = str(tmp_path / "thumb.jpg")
    result = derivative.write(path=path, data=b"bytes", local=True)
    assert result is None
    assert fs.read(path) == b"bytes"


def test_write_rejects_path_without_local_true(tmp_path):
    path = str(tmp_path / "thumb.jpg")
    with pytest.raises(ValueError, match="local=True"):
        derivative.write(path=path, data=b"bytes")


def test_write_rejects_local_true_without_path():
    with pytest.raises(ValueError, match="path"):
        derivative.write(data=b"bytes", local=True)


def test_write_rejects_local_true_mixed_with_cask_params(tmp_path):
    path = str(tmp_path / "thumb.jpg")
    with pytest.raises(ValueError, match="local=True"):
        derivative.write(path=path, data=b"bytes", local=True, type="silver")


def test_write_rejects_missing_cask_params_when_no_path(fake_fs):
    with pytest.raises(ValueError, match="type/collection/rel_path"):
        derivative.write(data=b"bytes")
