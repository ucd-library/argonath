import json

import pytest

from digtk.lib import showcase_index
from digtk.lib.items import ItemLocation

ITEM = ItemLocation(
    ark="ark:/87287/d7abc1234", group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
)
MANIFEST_PATH = "cask:/silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images/manifest.json"


@pytest.fixture
def manifest(monkeypatch):
    """Serve one manifest.json at the image's derivatives path; anything else is missing."""
    served = {}

    def fake_read(path):
        if path in served:
            return served[path]
        raise FileNotFoundError(path)
    monkeypatch.setattr(showcase_index.fs, "read", fake_read)

    def set_doc(doc):
        served[MANIFEST_PATH] = json.dumps(doc).encode()
    return set_doc


def test_tiled_url_reads_the_top_level_tiled_entry(manifest):
    """write_manifest() merges image.generate()'s extra content into the manifest's
    top level — there's no nested "extra" key to look under."""
    manifest({
        "outputs": ["small.jpg", "medium.jpg", "large.jpg", "tiled.tif"],
        "tiled": {
            "size": [{"width": "3072", "height": "4000"}],
            "url": "cask://silver/g/c/latest/item/public-frontend-derivatives/images/photo.tiff/images/tiled.tif",
        },
    })

    url = showcase_index._tiled_iiif_url_for_filename(ITEM, "photo.tiff")

    assert url == (
        "/svc:cask-iiif/silver%2Fg%2Fc%2Flatest%2Fitem%2Fpublic-frontend-derivatives"
        "%2Fimages%2Fphoto.tiff%2Fimages%2Ftiled.tif"
    )


def test_tiled_url_is_none_without_a_tiled_entry(manifest):
    manifest({"outputs": ["small.jpg"]})

    assert showcase_index._tiled_iiif_url_for_filename(ITEM, "photo.tiff") is None


def test_tiled_url_is_none_when_derivatives_were_never_generated(manifest):
    assert showcase_index._tiled_iiif_url_for_filename(ITEM, "photo.tiff") is None
