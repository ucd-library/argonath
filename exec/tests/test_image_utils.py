import shutil
import subprocess

import pytest

from digtk.lib import image_utils

needs_imagemagick = pytest.mark.skipif(
    shutil.which("convert") is None or shutil.which("identify") is None,
    reason="ImageMagick not installed",
)


def _identify(path, fmt):
    out = subprocess.run(
        ["identify", "-format", fmt, str(path)], capture_output=True, check=True
    )
    return out.stdout.decode("utf-8", errors="replace").strip()


def _dims(path):
    w, h = _identify(str(path) + "[0]", "%w %h").split()
    return int(w), int(h)


def _make_image(path, size="64x48", extra=()):
    subprocess.run(
        ["convert", "-size", size, "gradient:black-white", *extra, str(path)],
        capture_output=True, check=True,
    )


@pytest.fixture
def captured_args(monkeypatch):
    """Capture _run_convert args without running ImageMagick; bit depth pinned to 8."""
    calls = []
    monkeypatch.setattr(image_utils, "_run_convert", lambda args: calls.append(args))
    monkeypatch.setattr(image_utils, "_bit_depth", lambda src: 8)
    return calls


# --- argument assembly (no ImageMagick required) ------------------------------

def test_thumbnail_quality_opt(captured_args, tmp_path):
    image_utils.thumbnail("src.tiff", str(tmp_path / "t.jpg"), 256, opts={"quality": 50})
    args = captured_args[0]
    assert args[args.index("-quality") + 1] == "50"
    assert args[args.index("-thumbnail") + 1] == "256x>"


def test_thumbnail_auto_levels_deep_sources(captured_args, monkeypatch, tmp_path):
    monkeypatch.setattr(image_utils, "_bit_depth", lambda src: 16)
    image_utils.thumbnail("src.tiff", str(tmp_path / "t.jpg"), 256)
    assert "-auto-level" in captured_args[0]


def test_thumbnail_no_auto_level_for_8bit(captured_args, tmp_path):
    image_utils.thumbnail("src.tiff", str(tmp_path / "t.jpg"), 256)
    assert "-auto-level" not in captured_args[0]


def test_thumbnail_uses_first_frame_and_orients(captured_args, tmp_path):
    image_utils.thumbnail("src.tiff", str(tmp_path / "t.jpg"), 256)
    assert captured_args[0][0] == "src.tiff[0]"
    assert "-auto-orient" in captured_args[0]


def test_convert_format_quality_and_compress_opts(captured_args, tmp_path):
    image_utils.convert_format(
        "src.tiff", str(tmp_path / "o.png"), opts={"quality": 90, "compress": "LZW"}
    )
    args = captured_args[0]
    assert args[args.index("-quality") + 1] == "90"
    assert args[args.index("-compress") + 1] == "LZW"


def test_convert_format_defaults_add_no_flags(captured_args, tmp_path):
    image_utils.convert_format("src.tiff", str(tmp_path / "o.png"))
    assert captured_args[0] == ["src.tiff", str(tmp_path / "o.png")]


def test_deskew_threshold_opt(captured_args, tmp_path):
    image_utils.deskew("src.tiff", str(tmp_path / "d.tiff"), opts={"threshold": "80%"})
    args = captured_args[0]
    assert args[args.index("-deskew") + 1] == "80%"
    assert "+repage" in args


def test_prep_ocr_fuzz_opt_and_density(captured_args, tmp_path):
    image_utils.prep_ocr("src.tiff", str(tmp_path / "p.tiff"), opts={"fuzz": "15%"})
    args = captured_args[0]
    assert args[args.index("-fuzz") + 1] == "15%"
    assert args[args.index("-density") + 1] == "300"
    assert args[args.index("-resize") + 1] == "2048x2048>"


def test_creates_parent_dirs(captured_args, tmp_path):
    dest = tmp_path / "sub" / "deeper" / "t.jpg"
    image_utils.thumbnail("src.tiff", str(dest), 256)
    assert dest.parent.is_dir()


# --- end-to-end against real ImageMagick --------------------------------------

@needs_imagemagick
def test_thumbnail_resizes_to_width(tmp_path):
    src = tmp_path / "src.tiff"
    _make_image(src, size="64x48")
    dest = tmp_path / "thumb.jpg"
    image_utils.thumbnail(str(src), str(dest), 32)
    assert _dims(dest) == (32, 24)
    assert _identify(dest, "%m") == "JPEG"


@needs_imagemagick
def test_thumbnail_never_upscales(tmp_path):
    src = tmp_path / "src.tiff"
    _make_image(src, size="64x48")
    dest = tmp_path / "thumb.jpg"
    image_utils.thumbnail(str(src), str(dest), 500)
    assert _dims(dest) == (64, 48)


@needs_imagemagick
def test_convert_format_produces_target_format(tmp_path):
    src = tmp_path / "src.tiff"
    _make_image(src)
    dest = tmp_path / "out.png"
    image_utils.convert_format(str(src), str(dest))
    assert _identify(dest, "%m") == "PNG"


@needs_imagemagick
def test_deskew_writes_output(tmp_path):
    src = tmp_path / "src.tiff"
    _make_image(src)
    dest = tmp_path / "deskewed.tiff"
    image_utils.deskew(str(src), str(dest))
    assert dest.exists()


@needs_imagemagick
def test_prep_ocr_caps_long_edge(tmp_path):
    src = tmp_path / "src.tiff"
    _make_image(src, size="3000x1000")
    dest = tmp_path / "prep.tiff"
    image_utils.prep_ocr(str(src), str(dest))
    assert max(_dims(dest)) == 2048


@needs_imagemagick
def test_prep_ocr_never_upscales(tmp_path):
    src = tmp_path / "src.tiff"
    _make_image(src, size="64x48")
    dest = tmp_path / "prep.tiff"
    image_utils.prep_ocr(str(src), str(dest))
    assert _dims(dest) == (64, 48)


@needs_imagemagick
def test_missing_source_raises_runtime_error(tmp_path):
    with pytest.raises(RuntimeError, match="convert failed"):
        image_utils.thumbnail(str(tmp_path / "nope.tiff"), str(tmp_path / "t.jpg"), 32)


def test_prep_ocr_enhance_gray_flags(captured_args, tmp_path):
    image_utils.prep_ocr("src.tiff", str(tmp_path / "p.tiff"), opts={"enhance": "gray"})
    args = captured_args[0]
    assert args[args.index("-colorspace") + 1] == "Gray"
    assert args[args.index("-contrast-stretch") + 1] == "2%x2%"


def test_prep_ocr_enhance_auto_keeps_color(captured_args, tmp_path):
    image_utils.prep_ocr("src.tiff", str(tmp_path / "p.tiff"), opts={"enhance": "auto"})
    args = captured_args[0]
    assert "-contrast-stretch" in args
    assert "-colorspace" not in args


def test_prep_ocr_no_enhance_by_default(captured_args, tmp_path):
    image_utils.prep_ocr("src.tiff", str(tmp_path / "p.tiff"))
    assert "-contrast-stretch" not in captured_args[0]


def test_prep_ocr_bad_enhance_raises(captured_args, tmp_path):
    with pytest.raises(ValueError, match="unknown enhance mode"):
        image_utils.prep_ocr("src.tiff", str(tmp_path / "p.tiff"), opts={"enhance": "sharpen"})


@needs_imagemagick
def test_prep_ocr_enhance_gray_end_to_end(tmp_path):
    src = tmp_path / "src.tiff"
    _make_image(src, size="64x48")
    dest = tmp_path / "prep.tiff"
    image_utils.prep_ocr(str(src), str(dest), opts={"enhance": "gray"})
    assert _identify(dest, "%[colorspace]") == "Gray"
