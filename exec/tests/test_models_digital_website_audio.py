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
    audio_result = result["audios"]["interview.wav"]
    assert audio_result["outputs"] == ["converted.mp3"]
    assert audio_result["dir"] == "cask:/silver/g/c/latest/item/public-frontend-derivatives/interview.wav"
    write_call = fake_derivative[0]
    assert write_call["opts"]["link_relation"] == "https://lib.ucdavis.edu/argonath/ns#derivativeOf"


def test_transcode_defaults_to_configured_format(fake_fs, fake_derivative, fake_ffmpeg):
    result = aw.transcode(ARK, opts={})
    assert result["audios"]["interview.wav"]["outputs"] == ["converted.mp3"]  # default transcode_format is "mp3"


def test_waveform_writes_output_named_by_dimensions(fake_fs, fake_derivative, fake_ffmpeg):
    result = aw.waveform(ARK, opts={"width": 200, "height": 80})
    assert result["audios"]["interview.wav"]["outputs"] == ["waveform-200x80.png"]


def test_derivatives_writes_transcode_and_waveform_together(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    result = aw.derivatives(ARK, opts={})
    audio_result = result["derivatives"]["interview.wav"]
    assert sorted(audio_result["outputs"]) == sorted(["converted.mp3", "waveform-1024x256.png"])
    assert audio_result["dir"] == "cask:/silver/g/c/latest/item/public-frontend-derivatives/interview.wav"


def test_derivatives_never_writes_an_item_record(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    aw.derivatives(ARK, opts={})
    assert not any(c["rel_path"].endswith("item.jsonld.json") for c in fake_derivative)


def test_media_objects_builds_audio_object_from_published_manifest(monkeypatch):
    audio_dir = "cask:/silver/g/c/latest/item/public-frontend-derivatives/interview.wav"
    manifests = {f"{audio_dir}/manifest.json": json.dumps({"outputs": ["converted.mp3", "waveform-1024x256.png"]}).encode()}
    monkeypatch.setattr(aw.fs, "read", lambda path: manifests[path])

    media = aw.media_objects(ITEM)

    assert media == [{
        "@type": "AudioObject",
        "identifier": "interview",
        "contentUrl": "cask://silver/g/c/latest/item/public-frontend-derivatives/interview.wav/converted.mp3",
        "encodingFormat": "audio/mp3",
    }]


def test_media_objects_raises_without_a_transcoded_copy(monkeypatch):
    monkeypatch.setattr(aw.fs, "read", lambda path: json.dumps({"outputs": ["waveform-1024x256.png"]}).encode())

    with pytest.raises(FileNotFoundError, match="no transcoded audio"):
        aw.media_objects(ITEM)


def test_derivatives_multi_audio_item_nests_by_source_filename(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper, monkeypatch):
    use_multi_item(monkeypatch)
    result = aw.derivatives(ARK, opts={})
    assert set(result["derivatives"].keys()) == {"side-a.wav", "side-b.wav"}
    for filename in ("side-a.wav", "side-b.wav"):
        assert sorted(result["derivatives"][filename]["outputs"]) == sorted(["converted.mp3", "waveform-1024x256.png"])
        assert result["derivatives"][filename]["dir"] == (
            f"cask:/silver/g/c/latest/box-1/folder-3/public-frontend-derivatives/{filename}"
        )


def test_derivatives_transcribes_captions_for_every_source(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper):
    result = aw.derivatives(ARK, opts={})
    assert result["captions"]["dir"] == "cask:/silver/g/c/latest/item/captions"
    assert result["captions"]["outputs"] == ["output-en.vtt"]
    assert fake_whisper[0]["src_path"].endswith("interview.wav")


def test_derivatives_transcribes_every_source_for_multi_audio_item(fake_fs, fake_derivative, fake_ffmpeg, fake_whisper, monkeypatch):
    use_multi_item(monkeypatch)
    result = aw.derivatives(ARK, opts={})
    assert sorted(result["captions"]["outputs"]) == ["side-a-output-en.vtt", "side-b-output-en.vtt"]
