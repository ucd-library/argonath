import pytest

from digtk.lib import pg_workflow
from digtk.lib.items import ItemLocation

ARK = "ark:/87287/d7abc1234"

IMAGE_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tif"],
)

# Two TIFF page masters alongside a PDF -- needs the 'media' wrapper (see
# gcs_sync._needs_media_wrapper()), same shape test_gcs_sync.py's
# MULTI_IMAGE_ITEM uses.
MULTI_IMAGE_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo1.tif",
        "cask:/bronze/g/c/latest/item/photo2.tif",
        "cask:/bronze/g/c/latest/item/document.pdf",
    ],
)

# Two images (triggers the 'media' wrapper) plus a video -- video must never
# get wrapped regardless of the item's other binaries.
MULTI_IMAGE_WITH_VIDEO_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo1.tif",
        "cask:/bronze/g/c/latest/item/photo2.tif",
        "cask:/bronze/g/c/latest/item/movie.mp4",
    ],
)


class FakeCursor:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, sql, params):
        self.calls.append(params)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeConnection:
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.closed = False

    def cursor(self):
        return FakeCursor(self.calls)

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


@pytest.fixture
def fake_connection(monkeypatch):
    conn = FakeConnection()
    monkeypatch.setattr(pg_workflow, "_connect", lambda: conn)
    return conn


def _fake_resolve(item, monkeypatch):
    monkeypatch.setattr(pg_workflow.items, "resolve", lambda ark, opts={}: item)


def test_mark_rejects_invalid_state(fake_connection, monkeypatch):
    _fake_resolve(IMAGE_ITEM, monkeypatch)
    with pytest.raises(ValueError):
        pg_workflow.mark(ARK, "bogus-state")


def test_mark_single_image_item_upserts_one_row(fake_connection, monkeypatch):
    _fake_resolve(IMAGE_ITEM, monkeypatch)
    result = pg_workflow.mark(ARK, "completed")

    assert result["status"] == "success"
    assert result["marked"] == {"image": {"photo.tif": "/item/ark:/87287/d7abc1234/photo.tif"}}
    assert len(fake_connection.calls) == 1
    assert fake_connection.calls[0]["state"] == "completed"
    assert fake_connection.calls[0]["name"] == "image-products"
    assert fake_connection.commits == 1
    assert fake_connection.closed is True


def test_mark_skips_formats_the_item_does_not_have(fake_connection, monkeypatch):
    # A single-image item has no pdf/video binaries -- pipeline.filter_binaries_by_format()
    # raises ValueError for a format with zero matches, so mark() must
    # pre-filter to formats actually present rather than calling it blindly
    # for every entry in gcs_sync.WORKFLOW_NAME.
    _fake_resolve(IMAGE_ITEM, monkeypatch)
    result = pg_workflow.mark(ARK, "completed")
    assert set(result["marked"].keys()) == {"image"}


def test_mark_wraps_multi_image_binaries_under_media_images(fake_connection, monkeypatch):
    _fake_resolve(MULTI_IMAGE_ITEM, monkeypatch)
    result = pg_workflow.mark(ARK, "completed")

    assert result["marked"]["image"] == {
        "photo1.tif": "/item/ark:/87287/d7abc1234/media/images/photo1.tif",
        "photo2.tif": "/item/ark:/87287/d7abc1234/media/images/photo2.tif",
    }
    assert result["marked"]["pdf"] == {
        "document.pdf": "/item/ark:/87287/d7abc1234/media/document.pdf",
    }
    assert len(fake_connection.calls) == 3
    names = {call["name"] for call in fake_connection.calls}
    assert names == {"image-products", "pdf-image-products"}


def test_mark_never_wraps_video_even_when_images_are_wrapped(fake_connection, monkeypatch):
    _fake_resolve(MULTI_IMAGE_WITH_VIDEO_ITEM, monkeypatch)
    result = pg_workflow.mark(ARK, "completed")

    assert result["marked"]["video"] == {
        "movie.mp4": "/item/ark:/87287/d7abc1234/movie.mp4",
    }
    assert result["marked"]["image"] == {
        "photo1.tif": "/item/ark:/87287/d7abc1234/media/images/photo1.tif",
        "photo2.tif": "/item/ark:/87287/d7abc1234/media/images/photo2.tif",
    }


def test_mark_upserts_row_with_json_wrapped_data(fake_connection, monkeypatch):
    _fake_resolve(IMAGE_ITEM, monkeypatch)
    pg_workflow.mark(ARK, "error")

    from psycopg2.extras import Json

    params = fake_connection.calls[0]
    assert params["state"] == "error"
    assert isinstance(params["data"], Json)
    assert params["data"].adapted["finPath"] == "/item/ark:/87287/d7abc1234/photo.tif"
