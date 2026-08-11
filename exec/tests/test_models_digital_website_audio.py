import json
from contextlib import contextmanager

import pytest

from digtk.lib.items import ItemLocation
from digtk.models.digital_website import audio as aw
from digtk.models.digital_website import pipeline

ARK = "ark:/87287/d7abc1234"

ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/interview.wav"],
)

MULTI_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-1/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-1/folder-3/side-a.wav",
        "cask:/bronze/g/c/latest/box-1/folder-3/side-b.wav",
    ],
)


@pytest.fixture(autouse=True)
def fake_items(monkeypatch):
    monkeypatch.setattr(aw.items, "resolve", lambda ark, opts={}: ITEM)


def use_multi_item(monkeypatch):
    monkeypatch.setattr(aw.items, "resolve", lambda ark, opts={}: MULTI_ITEM)


@pytest.fixture
def fake_fs(monkeypatch):
    read_stream_calls = []

    @contextmanager
    def fake_read_stream(path, opts={}):
        read_stream_calls.append(path)
        yield iter([b"fake-audio-bytes"])

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
    calls = {"transcode_audio": [], "waveform": [], "probe": []}

    def fake_transcode_audio(src_path, dest_path, opts={}):
        calls["transcode_audio"].append({"src_path": src_path, "dest_path": dest_path, "opts": opts})
        with open(dest_path, "wb") as f:
            f.write(b"converted-bytes")
        return {"argv": [], "dest_path": dest_path}

    def fake_waveform(src_path, dest_path, opts={}):
        calls["waveform"].append({"src_path": src_path, "dest_path": dest_path, "opts": opts})
        with open(dest_path, "wb") as f:
            f.write(b"waveform-bytes")
        return {"argv": [], "dest_path": dest_path}

    def fake_probe(path, opts={}):
        calls["probe"].append(path)
        return {"duration_s": 30.0, "bit_rate": 320000, "streams": []}

    monkeypatch.setattr(aw.ffmpeg_utils, "transcode_audio", fake_transcode_audio)
    monkeypatch.setattr(aw.ffmpeg_utils, "waveform", fake_waveform)
    monkeypatch.setattr(aw.ffmpeg_utils, "probe", fake_probe)
    return calls


@pytest.fixture
def fake_whisper(monkeypatch):
    calls = []

    def fake_transcribe(src_path, dest_path, opts={}):
        calls.append({"src_path": src_path, "dest_path": dest_path, "opts": opts})
        with open(dest_path, "w") as f:
            f.write("WEBVTT\n")
        return {"argv": [], "dest_path": dest_path}

    monkeypatch.setattr(aw.captions.whisper_utils, "transcribe", fake_transcribe)
    return calls


def test_probe_reports_per_source(fake_fs, fake_ffmpeg):
    result = aw.probe(ARK)
    assert result["streams"] == {"interview": {"duration_s": 30.0, "bit_rate": 320000, "streams": []}}


def test_transcode_writes_converted_output(fake_fs, fake_derivative, fake_ffmpeg):
    result = aw.transcode(ARK, opts={"format": "mp3"})
    assert result["outputs"] == ["converted.mp3"]
    assert result["dir"] == "cask:/silver/g/c/latest/item/derivatives"
    write_call = fake_derivative[0]
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#derivativeOf"


def test_transcode_defaults_to_configured_format(fake_fs, fake_derivative, fake_ffmpeg):
    result = aw.transcode(ARK)
    assert result["outputs"] == ["converted.mp3"]  # default transcode_format is "mp3"


def test_waveform_writes_output_named_by_dimensions(fake_fs, fake_derivative, fake_ffmpeg):
    result = aw.waveform(ARK, opts={"width": 200, "height": 80})
    assert result["outputs"] == ["waveform-200x80.png"]


def test_derivatives_writes_transcode_and_waveform_together(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    result = aw.derivatives(ARK)
    assert sorted(result["derivatives"]["outputs"]) == sorted(["converted.mp3", "waveform-1024x256.png"])


def test_derivatives_writes_audio_object_with_identifier(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    aw.derivatives(ARK)
    sidecar_write = next(c for c in fake_derivative if c["rel_path"].endswith("item.jsonld.json"))
    doc = json.loads(sidecar_write["data"])
    assert len(doc["associatedMedia"]) == 1
    media = doc["associatedMedia"][0]
    assert media["@type"] == "AudioObject"
    assert media["identifier"] == "interview"
    assert media["contentUrl"] == "cask://silver/g/c/latest/item/derivatives/converted.mp3"
    assert media["encodingFormat"] == "audio/mp3"


def test_derivatives_multi_audio_item_prefixes_filenames(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper, monkeypatch):
    use_multi_item(monkeypatch)
    result = aw.derivatives(ARK)
    assert sorted(result["derivatives"]["outputs"]) == sorted([
        "side-a-converted.mp3", "side-a-waveform-1024x256.png",
        "side-b-converted.mp3", "side-b-waveform-1024x256.png",
    ])


def test_derivatives_transcribes_captions_for_every_source(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    result = aw.derivatives(ARK)
    assert result["captions"]["dir"] == "cask:/silver/g/c/latest/item/captions"
    assert result["captions"]["outputs"] == ["output-en.vtt"]
    assert fake_whisper[0]["src_path"].endswith("interview.wav")


def test_derivatives_transcribes_every_source_for_multi_audio_item(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper, monkeypatch):
    use_multi_item(monkeypatch)
    result = aw.derivatives(ARK)
    assert sorted(result["captions"]["outputs"]) == ["side-a-output-en.vtt", "side-b-output-en.vtt"]
