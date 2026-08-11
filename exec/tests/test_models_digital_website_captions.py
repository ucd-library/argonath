import json

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import captions
from digtk.models.digital_website import pipeline

ARK = "ark:/87287/d7abc1234"

ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/video.mp4"],
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-1/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-1/folder-3/clip-a.mp4",
        "cask:/bronze/g/c/latest/box-1/folder-3/clip-b.mp4",
    ],
)

COMMAND = ["digtk", "video", "derivatives", ARK]


def _sources(item):
    return [
        {"bronze_path": path, "local_path": f"/tmp/{path.rsplit('/', 1)[-1]}", "stem": path.rsplit("/", 1)[-1].rsplit(".", 1)[0]}
        for path in item.bronze_binary_paths
    ]


@pytest.fixture
def fake_fs(monkeypatch):
    state = {"sidecar": None}
    monkeypatch.setattr(captions.fs, "exists", lambda path: state["sidecar"] is not None)
    monkeypatch.setattr(captions.fs, "read", lambda path: state["sidecar"])
    return state


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

    monkeypatch.setattr(captions.derivative, "write", fake_write)
    return calls


@pytest.fixture
def fake_whisper(monkeypatch):
    calls = []

    def fake_transcribe(src_path, dest_path, opts={}):
        calls.append({"src_path": src_path, "dest_path": dest_path, "opts": opts})
        with open(dest_path, "w") as f:
            f.write("WEBVTT\n")
        return {"argv": [], "dest_path": dest_path}

    monkeypatch.setattr(captions.whisper_utils, "transcribe", fake_transcribe)
    return calls


def test_transcribe_sources_publishes_vtt_and_manifest(fake_fs, fake_derivative, fake_whisper, tmp_path):
    result = captions.transcribe_sources(ITEM, _sources(ITEM), str(tmp_path), COMMAND)
    assert result["dir"] == "cask:/silver/g/c/latest/item/captions"
    assert result["outputs"] == ["output-en.vtt"]
    write_call = fake_derivative[0]
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#captionOf"


def test_transcribe_sources_honors_language_opt(fake_fs, fake_derivative, fake_whisper, tmp_path):
    result = captions.transcribe_sources(ITEM, _sources(ITEM), str(tmp_path), COMMAND, opts={"language": "es"})
    assert result["outputs"] == ["output-es.vtt"]


def test_transcribe_sources_creates_placeholder_sidecar_entry_when_no_existing_media(fake_fs, fake_derivative, fake_whisper, tmp_path):
    captions.transcribe_sources(ITEM, _sources(ITEM), str(tmp_path), COMMAND)
    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert len(doc["associatedMedia"]) == 1
    entry = doc["associatedMedia"][0]
    assert entry["identifier"] == "video"
    assert entry["caption"]["encodingFormat"] == "text/vtt"
    assert entry["caption"]["inLanguage"] == "en"
    assert doc["argonath:fieldSources"]["caption"] == "ai"


def test_transcribe_sources_merges_into_existing_video_object(fake_fs, fake_derivative, fake_whisper, tmp_path):
    fake_fs["sidecar"] = json.dumps({
        "@id": ARK, "ark": ARK,
        "associatedMedia": [{
            "@type": "VideoObject", "identifier": "video",
            "contentUrl": "cask://silver/g/c/latest/item/stream/playlist.m3u8",
        }],
    }).encode()

    captions.transcribe_sources(ITEM, _sources(ITEM), str(tmp_path), COMMAND, opts={"language": "en"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert len(doc["associatedMedia"]) == 1
    media = doc["associatedMedia"][0]
    assert media["@type"] == "VideoObject"  # untouched
    assert media["contentUrl"] == "cask://silver/g/c/latest/item/stream/playlist.m3u8"  # untouched
    assert media["caption"]["contentUrl"] == "cask://silver/g/c/latest/item/captions/output-en.vtt"


def test_transcribe_sources_multi_source_matches_captions_to_correct_identifier(fake_fs, fake_derivative, fake_whisper, tmp_path):
    fake_fs["sidecar"] = json.dumps({
        "@id": ARK, "ark": ARK,
        "associatedMedia": [
            {"@type": "VideoObject", "identifier": "clip-a", "contentUrl": "cask://.../clip-a-playlist.m3u8"},
            {"@type": "VideoObject", "identifier": "clip-b", "contentUrl": "cask://.../clip-b-playlist.m3u8"},
        ],
    }).encode()

    captions.transcribe_sources(MULTI_ITEM, _sources(MULTI_ITEM), str(tmp_path), COMMAND)

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    by_id = {m["identifier"]: m for m in doc["associatedMedia"]}
    assert by_id["clip-a"]["caption"]["contentUrl"].endswith("clip-a-output-en.vtt")
    assert by_id["clip-b"]["caption"]["contentUrl"].endswith("clip-b-output-en.vtt")
