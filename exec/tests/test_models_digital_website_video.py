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
    calls = {"hls_ladder": [], "probe": []}

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

    def fake_probe(path, opts={}):
        calls["probe"].append(path)
        return {"duration_s": 10.0, "bit_rate": 1000, "streams": []}

    monkeypatch.setattr(vw.ffmpeg_utils, "hls_ladder", fake_hls_ladder)
    monkeypatch.setattr(vw.ffmpeg_utils, "probe", fake_probe)
    return calls


@pytest.fixture
def fake_whisper(monkeypatch):
    calls = []

    def fake_transcribe(src_path, dest_path, opts={}):
        calls.append({"src_path": src_path, "dest_path": dest_path, "opts": opts})
        with open(dest_path, "w") as f:
            f.write("WEBVTT\n")
        return {"argv": [], "dest_path": dest_path}

    monkeypatch.setattr(vw.captions.whisper_utils, "transcribe", fake_transcribe)
    return calls


def test_probe_reports_per_source(fake_fs, fake_ffmpeg):
    result = vw.probe(ARK)
    assert result == {"status": "success", "item": "/bronze/g/c/latest/item", "streams": {"video": {"duration_s": 10.0, "bit_rate": 1000, "streams": []}}}
    assert len(fake_ffmpeg["probe"]) == 1
    assert fake_ffmpeg["probe"][0].endswith("video.mp4")


def test_hls_publishes_every_rung_and_master_playlist(fake_fs, fake_derivative, fake_ffmpeg):
    result = vw.hls(ARK, opts={"resolutions": "360p,720p"})
    video_result = result["videos"]["video.mp4"]
    assert video_result["dir"] == "cask:/silver/g/c/latest/item/public-frontend-derivatives/video.mp4/streaming"
    assert sorted(video_result["outputs"]) == sorted(["360p.m3u8", "360p.mp4", "720p.m3u8", "720p.mp4", "playlist.m3u8"])
    write_call = fake_derivative[0]
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#streamOf"


def test_hls_uses_configured_default_resolutions(fake_fs, fake_derivative, fake_ffmpeg):
    vw.hls(ARK, opts={})
    assert fake_ffmpeg["hls_ladder"][0]["resolutions"] == ["360p", "480p", "720p", "1080p"]


def test_hls_nests_per_source_for_multi_video_item(fake_fs, fake_derivative, fake_ffmpeg, monkeypatch):
    use_multi_item(monkeypatch)
    result = vw.hls(ARK, opts={"resolutions": "360p"})
    assert set(result["videos"].keys()) == {"clip-a.mp4", "clip-b.mp4"}
    for filename in ("clip-a.mp4", "clip-b.mp4"):
        assert result["videos"][filename]["dir"] == (
            f"cask:/silver/g/c/latest/box-1/folder-3/public-frontend-derivatives/{filename}/streaming"
        )
        assert "playlist.m3u8" in result["videos"][filename]["outputs"]


def test_derivatives_publishes_stream_per_source(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    result = vw.derivatives(ARK, opts={"resolutions": "360p"})
    stream_result = result["stream"]["video.mp4"]
    assert stream_result["dir"] == "cask:/silver/g/c/latest/item/public-frontend-derivatives/video.mp4/streaming"
    assert "playlist.m3u8" in stream_result["outputs"]


def test_derivatives_never_writes_an_item_record(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    vw.derivatives(ARK, opts={"resolutions": "360p"})
    assert not any(c["rel_path"].endswith("item.jsonld.json") for c in fake_derivative)


def test_media_objects_builds_video_object_from_published_manifest(monkeypatch):
    stream_dir = "cask:/silver/g/c/latest/item/public-frontend-derivatives/video.mp4/streaming"
    manifests = {f"{stream_dir}/manifest.json": json.dumps({"outputs": ["360p.m3u8", "playlist.m3u8"]}).encode()}
    monkeypatch.setattr(pipeline.fs, "read", lambda path: manifests[path])

    media = vw.media_objects(ITEM)

    assert media == [{
        "@type": "VideoObject",
        "identifier": "video",
        "contentUrl": "cask://silver/g/c/latest/item/public-frontend-derivatives/video.mp4/streaming/playlist.m3u8",
        "encodingFormat": "application/vnd.apple.mpegurl",
    }]


def test_media_objects_raises_without_a_playlist(monkeypatch):
    monkeypatch.setattr(pipeline.fs, "read", lambda path: json.dumps({"outputs": ["360p.m3u8"]}).encode())

    with pytest.raises(FileNotFoundError, match="no HLS playlist"):
        vw.media_objects(ITEM)


def test_derivatives_multi_video_item_nests_by_source_filename(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper, monkeypatch):
    use_multi_item(monkeypatch)
    result = vw.derivatives(ARK, opts={"resolutions": "360p"})
    assert set(result["stream"].keys()) == {"clip-a.mp4", "clip-b.mp4"}
    for filename in ("clip-a.mp4", "clip-b.mp4"):
        assert result["stream"][filename]["dir"] == (
            f"cask:/silver/g/c/latest/box-1/folder-3/public-frontend-derivatives/{filename}/streaming"
        )
        assert "playlist.m3u8" in result["stream"][filename]["outputs"]


def test_derivatives_transcribes_captions_for_every_source(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    result = vw.derivatives(ARK, opts={"resolutions": "360p"})
    assert result["captions"]["dir"] == "cask:/silver/g/c/latest/item/captions"
    assert result["captions"]["outputs"] == ["output-en.vtt"]
    assert fake_whisper[0]["src_path"].endswith("video.mp4")


def test_derivatives_transcribes_every_source_for_multi_video_item(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper, monkeypatch):
    use_multi_item(monkeypatch)
    result = vw.derivatives(ARK, opts={"resolutions": "360p"})
    assert sorted(result["captions"]["outputs"]) == ["clip-a-output-en.vtt", "clip-b-output-en.vtt"]
