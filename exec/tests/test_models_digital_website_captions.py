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
    files = {}
    monkeypatch.setattr(captions.fs, "exists", lambda path: path in files)
    monkeypatch.setattr(captions.fs, "read", lambda path: files[path])
    return files


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
    result = captions.transcribe_sources(ITEM, _sources(ITEM), str(tmp_path), COMMAND, opts={})
    assert result["dir"] == "cask:/silver/g/c/latest/item/captions"
    assert result["outputs"] == ["output-en.vtt"]
    write_call = fake_derivative[0]
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#captionOf"


def test_transcribe_sources_honors_language_opt(fake_fs, fake_derivative, fake_whisper, tmp_path):
    result = captions.transcribe_sources(
        ITEM, _sources(ITEM), str(tmp_path), COMMAND, opts={"language": "es"},
    )
    assert result["outputs"] == ["output-es.vtt"]


def test_transcribe_sources_never_writes_an_item_record(fake_fs, fake_derivative, fake_whisper, tmp_path):
    captions.transcribe_sources(ITEM, _sources(ITEM), str(tmp_path), COMMAND, opts={})
    assert not any(c["rel_path"].endswith("item.jsonld.json") for c in fake_derivative)


def _captions_manifest(fake_fs, item, outputs, language="en"):
    rel = f"{item.item_path}/captions" if item.item_path else "captions"
    fake_fs[f"cask:/silver/g/c/latest/{rel}/manifest.json"] = json.dumps({
        "command": COMMAND, "params": {"language": language}, "outputs": outputs,
    }).encode()


def test_attach_captions_adds_caption_to_matching_entry(fake_fs):
    _captions_manifest(fake_fs, ITEM, ["output-en.vtt"])
    entries = [{
        "@type": "VideoObject", "identifier": "video",
        "contentUrl": "cask://silver/g/c/latest/item/stream/playlist.m3u8",
    }]

    assert captions.attach_captions(ITEM, entries) is True
    assert entries[0]["contentUrl"] == "cask://silver/g/c/latest/item/stream/playlist.m3u8"
    assert entries[0]["caption"] == {
        "@type": "MediaObject", "encodingFormat": "text/vtt", "inLanguage": "en",
        "contentUrl": "cask://silver/g/c/latest/item/captions/output-en.vtt",
    }


def test_attach_captions_multi_source_matches_captions_to_correct_identifier(fake_fs):
    _captions_manifest(fake_fs, MULTI_ITEM, ["clip-a-output-en.vtt", "clip-b-output-en.vtt"])
    entries = [
        {"@type": "VideoObject", "identifier": "clip-a", "contentUrl": "cask://.../clip-a-playlist.m3u8"},
        {"@type": "VideoObject", "identifier": "clip-b", "contentUrl": "cask://.../clip-b-playlist.m3u8"},
    ]

    captions.attach_captions(MULTI_ITEM, entries)

    by_id = {m["identifier"]: m for m in entries}
    assert by_id["clip-a"]["caption"]["contentUrl"].endswith("clip-a-output-en.vtt")
    assert by_id["clip-b"]["caption"]["contentUrl"].endswith("clip-b-output-en.vtt")


def test_attach_captions_without_a_captions_manifest_attaches_nothing(fake_fs):
    entries = [{"@type": "VideoObject", "identifier": "video", "contentUrl": "cask://.../playlist.m3u8"}]

    assert captions.attach_captions(ITEM, entries) is False
    assert "caption" not in entries[0]
