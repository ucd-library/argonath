import json
import os
from contextlib import contextmanager

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import pipeline
from digtk.models.digital_website import video as vw

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


@pytest.fixture(autouse=True)
def fake_items(monkeypatch):
    monkeypatch.setattr(vw.items, "resolve", lambda ark, opts={}: ITEM)


def use_multi_item(monkeypatch):
    monkeypatch.setattr(vw.items, "resolve", lambda ark, opts={}: MULTI_ITEM)


@pytest.fixture
def fake_fs(monkeypatch):
    read_stream_calls = []

    @contextmanager
    def fake_read_stream(path, opts={}):
        read_stream_calls.append(path)
        yield iter([b"fake-video-bytes"])

    monkeypatch.setattr(pipeline.fs, "read_stream", fake_read_stream)
    monkeypatch.setattr(pipeline.fs, "exists", lambda path: False)
    monkeypatch.setattr(pipeline.fs, "read", lambda path: b"{}")
    return {"read_stream_calls": read_stream_calls}


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
def fake_ffmpeg(monkeypatch):
    calls = {"hls_ladder": [], "extract_poster": [], "probe": []}

    def fake_hls_ladder(src_path, dest_dir, resolutions, opts={}):
        calls["hls_ladder"].append({"src_path": src_path, "resolutions": resolutions})
        rungs = []
        for res in resolutions:
            playlist = os.path.join(dest_dir, f"{res}.m3u8")
            segment = os.path.join(dest_dir, f"{res}.mp4")
            with open(playlist, "wb") as f:
                f.write(b"m3u8-data")
            with open(segment, "wb") as f:
                f.write(b"mp4-data")
            rungs.append({"argv": [], "playlist_path": playlist, "segment_path": segment})
        master = os.path.join(dest_dir, "playlist.m3u8")
        with open(master, "wb") as f:
            f.write(b"master-data")
        return {"rungs": rungs, "master_playlist_path": master}

    def fake_extract_poster(src_path, dest_path, opts={}):
        calls["extract_poster"].append({"src_path": src_path, "dest_path": dest_path, "opts": opts})
        with open(dest_path, "wb") as f:
            f.write(b"poster-data")
        return {"argv": [], "dest_path": dest_path}

    def fake_probe(path, opts={}):
        calls["probe"].append(path)
        return {"duration_s": 10.0, "bit_rate": 1000, "streams": []}

    monkeypatch.setattr(vw.ffmpeg_utils, "hls_ladder", fake_hls_ladder)
    monkeypatch.setattr(vw.ffmpeg_utils, "extract_poster", fake_extract_poster)
    monkeypatch.setattr(vw.ffmpeg_utils, "probe", fake_probe)
    return calls


def test_probe_reports_per_source(fake_fs, fake_ffmpeg):
    result = vw.probe(ARK)
    assert result == {"status": "success", "ark": ARK, "streams": {"video": {"duration_s": 10.0, "bit_rate": 1000, "streams": []}}}
    assert len(fake_ffmpeg["probe"]) == 1
    assert fake_ffmpeg["probe"][0].endswith("video.mp4")


def test_hls_publishes_every_rung_and_master_playlist(fake_fs, fake_derivative, fake_ffmpeg):
    result = vw.hls(ARK, opts={"resolutions": "360p,720p"})
    assert result["dir"] == "cask:/silver/g/c/latest/item/stream"
    assert sorted(result["outputs"]) == sorted(["360p.m3u8", "360p.mp4", "720p.m3u8", "720p.mp4", "playlist.m3u8"])
    write_call = fake_derivative[0]
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#streamOf"


def test_hls_uses_configured_default_resolutions(fake_fs, fake_derivative, fake_ffmpeg):
    vw.hls(ARK)
    assert fake_ffmpeg["hls_ladder"][0]["resolutions"] == ["360p", "480p", "720p", "1080p"]


def test_poster_publishes_to_derivatives(fake_fs, fake_derivative, fake_ffmpeg):
    result = vw.poster(ARK, opts={"width": 512})
    assert result["dir"] == "cask:/silver/g/c/latest/item/derivatives"
    assert result["outputs"] == ["poster-512.jpg"]


def test_derivatives_publishes_stream_and_poster_separately(fake_fs, fake_derivative, fake_ffmpeg):
    result = vw.derivatives(ARK, opts={"resolutions": "360p"})
    assert result["stream"]["dir"] == "cask:/silver/g/c/latest/item/stream"
    assert result["derivatives"]["dir"] == "cask:/silver/g/c/latest/item/derivatives"
    assert result["derivatives"]["outputs"] == ["poster-1024.jpg"]  # default poster_width is 1024


def test_derivatives_writes_video_object_with_identifier_and_thumbnail(fake_fs, fake_derivative, fake_ffmpeg):
    vw.derivatives(ARK, opts={"resolutions": "360p"})

    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert len(doc["associatedMedia"]) == 1
    media = doc["associatedMedia"][0]
    assert media["@type"] == "VideoObject"
    assert media["identifier"] == "video"
    assert media["contentUrl"] == "cask://silver/g/c/latest/item/stream/playlist.m3u8"
    assert media["thumbnailUrl"] == "cask://silver/g/c/latest/item/derivatives/poster-1024.jpg"


def test_derivatives_multi_video_item_prefixes_filenames(fake_fs, fake_derivative, fake_ffmpeg, monkeypatch):
    use_multi_item(monkeypatch)
    result = vw.derivatives(ARK, opts={"resolutions": "360p"})
    assert sorted(result["derivatives"]["outputs"]) == ["clip-a-poster-1024.jpg", "clip-b-poster-1024.jpg"]
    assert "clip-a-playlist.m3u8" in result["stream"]["outputs"]
    assert "clip-b-playlist.m3u8" in result["stream"]["outputs"]
