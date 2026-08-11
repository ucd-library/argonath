import json
import os
from contextlib import contextmanager

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import pipeline

ARK = "ark:/87287/d7abc1234"

ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tiff"],
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-1/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-a.tiff",
        "cask:/bronze/g/c/latest/box-1/folder-3/photo-b.tiff",
    ],
)

# A real shape: an archival transfer supplying both a compiled PDF and per-page TIFF
# masters for the same physical document, both directly from the archiving team (see
# docs/cask-conventions.md) — neither format derived from the other.
MIXED_FORMAT_PATHS = [
    "cask:/bronze/g/c/latest/item/document.pdf",
    "cask:/bronze/g/c/latest/item/page-001.tif",
    "cask:/bronze/g/c/latest/item/page-002.tif",
]


def test_filter_binaries_by_format_returns_only_matching_extension():
    assert pipeline.filter_binaries_by_format(MIXED_FORMAT_PATHS, "pdf") == ["cask:/bronze/g/c/latest/item/document.pdf"]
    assert pipeline.filter_binaries_by_format(MIXED_FORMAT_PATHS, "image") == [
        "cask:/bronze/g/c/latest/item/page-001.tif", "cask:/bronze/g/c/latest/item/page-002.tif",
    ]


def test_filter_binaries_by_format_accepts_a_set_of_formats():
    paths = ["cask:/bronze/g/c/latest/item/movie.mp4", "cask:/bronze/g/c/latest/item/interview.wav"]
    assert pipeline.filter_binaries_by_format(paths, {"video", "audio"}) == paths


def test_filter_binaries_by_format_raises_when_nothing_matches():
    with pytest.raises(ValueError, match="no audio binaries found"):
        pipeline.filter_binaries_by_format(MIXED_FORMAT_PATHS, "audio")


def test_filter_binaries_by_format_raises_on_unrecognized_extension():
    with pytest.raises(ValueError, match="unrecognized media extension"):
        pipeline.filter_binaries_by_format(["cask:/bronze/g/c/latest/item/mystery.xyz"], "image")

# generate_thumbnail_set(), select_orientation(), and update_associated_media() are
# already exercised end-to-end via test_models_digital_website_image.py's dw.generate() tests
# (same code, invoked through the still-image-specific orchestrator on top) — this file
# focuses on the pure helpers and on publish()'s link_relation behavior, which changed
# during the extraction from digital_website/image.py's private _publish().


def test_parse_widths_sorts_and_parses_comma_separated_string():
    assert pipeline.parse_widths("1024,256,512") == [256, 512, 1024]


def test_parse_widths_passes_through_a_list():
    assert pipeline.parse_widths([512, 256]) == [256, 512]


def test_output_name_prefixes_only_when_multi():
    assert pipeline.output_name("photo-a", "thumb-256.jpg", multi=True) == "photo-a-thumb-256.jpg"
    assert pipeline.output_name("photo", "thumb-256.jpg", multi=False) == "thumb-256.jpg"


def test_cask_public_ref_converts_dispatch_prefix():
    internal = "cask:/silver/g/c/latest/item/derivatives/thumb-256.jpg"
    assert pipeline.cask_public_ref(internal) == "cask://silver/g/c/latest/item/derivatives/thumb-256.jpg"


def test_generate_thumbnail_set_names_by_semantic_size(tmp_path, monkeypatch):
    calls = []

    def fake_thumbnail(src, dest, width, opts={}):
        calls.append(width)
        with open(dest, "wb") as f:
            f.write(b"fake")

    monkeypatch.setattr(pipeline.image_magick, "thumbnail", fake_thumbnail)
    outputs = pipeline.generate_thumbnail_set("src.jpg", str(tmp_path), [256, 512, 1024], "photo", {})

    assert [filename for filename, _ in outputs] == ["small.jpg", "medium.jpg", "large.jpg"]
    assert calls == [256, 512, 1024]
    for filename, local_path in outputs:
        assert os.path.basename(local_path) != filename  # local scratch name stays stem-disambiguated
        assert os.path.exists(local_path)


def test_generate_thumbnail_set_requires_exactly_three_widths():
    with pytest.raises(ValueError, match="requires exactly 3 widths"):
        pipeline.generate_thumbnail_set("src.jpg", "/tmp", [256, 512], "photo", {})


@pytest.fixture
def fake_fs(monkeypatch):
    read_stream_calls = []

    @contextmanager
    def fake_read_stream(path, opts={}):
        read_stream_calls.append(path)
        yield iter([b"fake-source-bytes"])

    monkeypatch.setattr(pipeline.fs, "read_stream", fake_read_stream)
    return {"read_stream_calls": read_stream_calls}


def test_stage_sources_streams_every_binary_and_derives_stem(fake_fs, tmp_path):
    sources = pipeline.stage_sources(MULTI_ITEM, str(tmp_path))
    assert [s["stem"] for s in sources] == ["photo-a", "photo-b"]
    assert sorted(fake_fs["read_stream_calls"]) == sorted(MULTI_ITEM.bronze_binary_paths)
    for s in sources:
        with open(s["local_path"], "rb") as f:
            assert f.read() == b"fake-source-bytes"


def test_stage_sources_honors_explicit_bronze_binary_paths_override(fake_fs, tmp_path):
    mixed_item = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=MIXED_FORMAT_PATHS,
    )
    pdf_only = pipeline.filter_binaries_by_format(MIXED_FORMAT_PATHS, "pdf")

    sources = pipeline.stage_sources(mixed_item, str(tmp_path), opts={"bronze_binary_paths": pdf_only})

    assert [s["stem"] for s in sources] == ["document"]
    assert fake_fs["read_stream_calls"] == ["cask:/bronze/g/c/latest/item/document.pdf"]


@pytest.fixture
def fake_derivative(monkeypatch):
    calls = []

    def fake_write(**kwargs):
        calls.append(kwargs)
        dest = pipeline.derivative.dest_path(
            type=kwargs["type"], collection=kwargs["collection"],
            rel_path=kwargs["rel_path"], opts=kwargs.get("opts", {}),
        )
        return {"path": f"cask:{dest}", "links": []}

    monkeypatch.setattr(pipeline.derivative, "write", fake_write)
    return calls


def test_publish_omits_link_relation_when_not_given(fake_derivative, tmp_path):
    local_path = tmp_path / "thumb-256.jpg"
    local_path.write_bytes(b"data")

    pipeline.publish(
        ITEM, "derivatives",
        [("thumb-256.jpg", str(local_path), "cask:/bronze/g/c/latest/item/photo.tiff")],
        command=["digtk", "image", "thumbnail", ARK], params={},
    )
    assert "link_relation" not in fake_derivative[0]["opts"]


def test_publish_forwards_explicit_link_relation(fake_derivative, tmp_path):
    local_path = tmp_path / "page-000" / "thumb-256.jpg"
    local_path.parent.mkdir()
    local_path.write_bytes(b"data")

    pipeline.publish(
        ITEM, "derivatives/page-000",
        [("thumb-256.jpg", str(local_path), "cask:/bronze/g/c/latest/item/photo.tiff")],
        command=["digtk", "pdf", "derivatives", ARK], params={},
        opts={"link_relation": "https://lib.ucdavis.edu/argonath/ns#derivativeOf"},
    )
    assert fake_derivative[0]["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#derivativeOf"
    assert fake_derivative[0]["rel_path"] == "item/derivatives/page-000/thumb-256.jpg"


def test_publish_records_manifest_and_source_files(fake_derivative, tmp_path):
    local_path = tmp_path / "thumb-256.jpg"
    local_path.write_bytes(b"data")

    result = pipeline.publish(
        ITEM, "derivatives",
        [("thumb-256.jpg", str(local_path), "cask:/bronze/g/c/latest/item/photo.tiff")],
        command=["digtk", "image", "thumbnail", ARK], params={"widths": [256]},
    )
    assert result["outputs"] == ["thumb-256.jpg"]
    assert result["dir"] == "cask:/silver/g/c/latest/item/derivatives"
    manifest_call = next(c for c in fake_derivative if c["rel_path"].endswith("manifest.json"))
    manifest = json.loads(manifest_call["data"])
    assert manifest["command"] == ["digtk", "image", "thumbnail", ARK]
    assert manifest["params"] == {"widths": [256]}


def test_update_associated_media_creates_new_sidecar(fake_derivative, monkeypatch):
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: False)
    media = [{"@type": "ImageObject", "contentUrl": "cask://silver/g/c/latest/item/derivatives/thumb-256.jpg"}]

    doc = pipeline.update_associated_media(ITEM, media, ["cask:/bronze/g/c/latest/item/photo.tiff"], "image")
    assert doc["@id"] == ARK
    assert doc["associatedMedia"] == media
    assert doc["associatedMedia"][0]["argonath:sourceGroup"] == "image"


def test_update_associated_media_preserves_other_source_groups(fake_derivative, monkeypatch):
    existing_doc = {
        "@id": ARK, "ark": ARK,
        "associatedMedia": [
            {"@type": "ImageObject", "contentUrl": "cask://.../thumb-256.jpg", "argonath:sourceGroup": "image"},
        ],
    }
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: True)
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps(existing_doc).encode())

    pdf_media = [{"@type": "ImageObject", "contentUrl": "cask://.../page-000/thumb-256.jpg"}]
    doc = pipeline.update_associated_media(ITEM, pdf_media, ["cask:/bronze/g/c/latest/item/document.pdf"], "pdf")

    groups = {e["argonath:sourceGroup"] for e in doc["associatedMedia"]}
    assert groups == {"image", "pdf"}
    assert len(doc["associatedMedia"]) == 2


def test_update_associated_media_replaces_only_matching_source_group(fake_derivative, monkeypatch):
    existing_doc = {
        "@id": ARK, "ark": ARK,
        "associatedMedia": [
            {"@type": "ImageObject", "contentUrl": "cask://.../old-thumb-256.jpg", "argonath:sourceGroup": "image"},
            {"@type": "ImageObject", "contentUrl": "cask://.../page-000/thumb-256.jpg", "argonath:sourceGroup": "pdf"},
        ],
    }
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: True)
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps(existing_doc).encode())

    new_image_media = [{"@type": "ImageObject", "contentUrl": "cask://.../new-thumb-256.jpg"}]
    doc = pipeline.update_associated_media(ITEM, new_image_media, ["cask:/bronze/g/c/latest/item/photo.tiff"], "image")

    by_group = {e["argonath:sourceGroup"]: e for e in doc["associatedMedia"]}
    assert by_group["image"]["contentUrl"] == "cask://.../new-thumb-256.jpg"
    assert by_group["pdf"]["contentUrl"] == "cask://.../page-000/thumb-256.jpg"
