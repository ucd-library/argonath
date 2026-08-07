import subprocess

import pytest

from digtk.lib import image_magick


class FakeCompletedProcess:
    def __init__(self, stdout=""):
        self.stdout = stdout


@pytest.fixture
def fake_run(monkeypatch):
    """Replaces subprocess.run with a recorder that returns canned stdout."""
    calls = []

    def _install(stdout="", raises=None):
        def fake(argv, **kwargs):
            calls.append({"argv": argv, "kwargs": kwargs})
            if raises is not None:
                raise raises
            return FakeCompletedProcess(stdout=stdout)
        monkeypatch.setattr(subprocess, "run", fake)
        return calls

    return _install


def test_probe_dimensions_parses_identify_output(fake_run):
    calls = fake_run(stdout="1024x768 ")
    result = image_magick.probe_dimensions("/tmp/photo.jpg")
    assert result == {"width": 1024, "height": 768}
    assert calls[0]["argv"] == ["identify", "-format", "%wx%h ", "/tmp/photo.jpg"]


def test_probe_dimensions_appends_explicit_frame(fake_run):
    calls = fake_run(stdout="100x200 ")
    image_magick.probe_dimensions("/tmp/scan.tif", opts={"frame": 2})
    assert calls[0]["argv"][-1] == "/tmp/scan.tif[2]"


def test_probe_dimensions_defaults_tiff_to_frame_zero(fake_run):
    calls = fake_run(stdout="100x200 ")
    image_magick.probe_dimensions("/tmp/scan.tiff")
    assert calls[0]["argv"][-1] == "/tmp/scan.tiff[0]"


def test_probe_dimensions_does_not_frame_index_non_tiff(fake_run):
    calls = fake_run(stdout="100x200 ")
    image_magick.probe_dimensions("/tmp/photo.jpg")
    assert calls[0]["argv"][-1] == "/tmp/photo.jpg"


def test_thumbnail_includes_resize_flag(fake_run):
    calls = fake_run()
    result = image_magick.thumbnail("/tmp/src.jpg", "/tmp/thumb-256.jpg", 256)
    argv = calls[0]["argv"]
    assert argv == [
        "convert", "/tmp/src.jpg",
        "-resize", "256x",
        "-quality", "90", "-alpha", "remove", "-layers", "flatten",
        "/tmp/thumb-256.jpg",
    ]
    assert result == {"argv": argv, "dest_path": "/tmp/thumb-256.jpg"}


def test_thumbnail_frame_indexes_tiff_sources(fake_run):
    calls = fake_run()
    image_magick.thumbnail("/tmp/src.tif", "/tmp/dest.jpg", 256)
    assert calls[0]["argv"][1] == "/tmp/src.tif[0]"


def test_thumbnail_honors_custom_quality(fake_run):
    calls = fake_run()
    image_magick.thumbnail("/tmp/src.jpg", "/tmp/dest.jpg", 256, opts={"quality": 75})
    argv = calls[0]["argv"]
    assert argv[argv.index("-quality") + 1] == "75"


def test_convert_format_skips_resize_flag(fake_run):
    calls = fake_run()
    result = image_magick.convert_format("/tmp/src.tif", "/tmp/dest.jpg")
    argv = calls[0]["argv"]
    assert argv == [
        "convert", "/tmp/src.tif[0]",
        "-quality", "90", "-alpha", "remove", "-layers", "flatten",
        "/tmp/dest.jpg",
    ]
    assert result == {"argv": argv, "dest_path": "/tmp/dest.jpg"}


def test_convert_format_honors_custom_quality(fake_run):
    calls = fake_run()
    image_magick.convert_format("/tmp/src.jpg", "/tmp/dest.jpg", opts={"quality": 75})
    argv = calls[0]["argv"]
    assert argv[argv.index("-quality") + 1] == "75"


def test_deskew_builds_two_stage_resize_recipe(fake_run):
    calls = fake_run()
    result = image_magick.deskew("/tmp/src.jpg", "/tmp/large-deskew.jpg")
    assert calls[0]["argv"] == [
        "convert", "/tmp/src.jpg",
        "-resize", "2048x",
        "-deskew", "40%",
        "-quality", "90",
        "-alpha", "remove",
        "-layers", "flatten",
        "-resize", "1024x",
        "/tmp/large-deskew.jpg",
    ]
    assert result["dest_path"] == "/tmp/large-deskew.jpg"


def test_deskew_honors_custom_threshold_and_output_width(fake_run):
    calls = fake_run()
    image_magick.deskew("/tmp/src.jpg", "/tmp/dest.jpg", opts={"threshold": 25, "output_width": 512})
    argv = calls[0]["argv"]
    assert "-deskew" in argv and argv[argv.index("-deskew") + 1] == "25%"
    assert argv[-2] == "512x"


def test_prep_ocr_without_deskew(fake_run):
    calls = fake_run()
    result = image_magick.prep_ocr("/tmp/src.jpg", "/tmp/ocr.jpg")
    assert calls[0]["argv"] == [
        "convert", "/tmp/src.jpg",
        "-resize", "2048x",
        "-density", "300",
        "-alpha", "remove",
        "-fill", "black",
        "-fuzz", "80%",
        "+opaque", "#FFFFFF",
        "-filter", "catrom",
        "-layers", "flatten",
        "-quality", "100",
        "/tmp/ocr.jpg",
    ]
    assert result["dest_path"] == "/tmp/ocr.jpg"


def test_prep_ocr_with_deskew_inserts_deskew_flag_after_resize(fake_run):
    calls = fake_run()
    image_magick.prep_ocr("/tmp/src.jpg", "/tmp/ocr-deskew.jpg", opts={"deskew": True})
    argv = calls[0]["argv"]
    assert argv == [
        "convert", "/tmp/src.jpg",
        "-resize", "2048x",
        "-deskew", "40%",
        "-density", "300",
        "-alpha", "remove",
        "-fill", "black",
        "-fuzz", "80%",
        "+opaque", "#FFFFFF",
        "-filter", "catrom",
        "-layers", "flatten",
        "-quality", "100",
        "/tmp/ocr-deskew.jpg",
    ]


def test_prep_ocr_honors_custom_fuzz_and_dpi(fake_run):
    calls = fake_run()
    image_magick.prep_ocr("/tmp/src.jpg", "/tmp/dest.jpg", opts={"fuzz": 50, "dpi": 600})
    argv = calls[0]["argv"]
    assert argv[argv.index("-fuzz") + 1] == "50%"
    assert argv[argv.index("-density") + 1] == "600"


def test_run_never_uses_shell(fake_run):
    calls = fake_run()
    image_magick.convert_format("/tmp/src.jpg", "/tmp/dest.jpg")
    assert calls[0]["kwargs"].get("shell", False) is False


def test_run_raises_image_magick_error_on_nonzero_exit(fake_run):
    fake_run(raises=subprocess.CalledProcessError(1, ["convert"], stderr="bad image"))
    with pytest.raises(image_magick.ImageMagickError) as exc_info:
        image_magick.convert_format("/tmp/src.jpg", "/tmp/dest.jpg")
    err = exc_info.value
    assert err.returncode == 1
    assert err.stderr == "bad image"
    assert err.timed_out is False


def test_run_raises_image_magick_error_on_timeout(fake_run):
    fake_run(raises=subprocess.TimeoutExpired(cmd=["convert"], timeout=120))
    with pytest.raises(image_magick.ImageMagickError) as exc_info:
        image_magick.convert_format("/tmp/src.jpg", "/tmp/dest.jpg")
    assert exc_info.value.timed_out is True


def test_run_passes_configured_timeout(fake_run):
    calls = fake_run()
    image_magick.convert_format("/tmp/src.jpg", "/tmp/dest.jpg")
    assert calls[0]["kwargs"]["timeout"] == 120.0


def test_run_honors_opts_timeout_override(fake_run):
    calls = fake_run()
    image_magick.convert_format("/tmp/src.jpg", "/tmp/dest.jpg", opts={"timeout": 5})
    assert calls[0]["kwargs"]["timeout"] == 5
