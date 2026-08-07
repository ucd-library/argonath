"""
ImageMagick subprocess wrapper.

All operations shell out to ImageMagick's `convert`/`identify` binaries via
subprocess, using list-args (never shell=True) and check=True, so a failed
step raises immediately instead of silently letting a multi-step recipe
continue against a stale or missing file — see the phase 0.4 migration notes
for the production bug class (from the reference dams/services/image-utils
implementation) this is deliberately fixing, not replicating.

This module is ARK/CaskFS-agnostic: every function takes and returns local
filesystem paths only. digtk.models.digital_website is responsible for
staging CaskFS sources to local paths before calling in here, and publishing
local outputs back to CaskFS afterward.
"""
import subprocess

from digtk.lib.config import CONFIG


class ImageMagickError(RuntimeError):
    """Raised when an ImageMagick subprocess invocation fails or times out.

    :param list[str] argv: The command that was run.
    :param int|None returncode: Process exit code, or None if it timed out.
    :param str stderr: Captured stderr output.
    :param bool timed_out: True if the failure was a timeout rather than a non-zero exit.
    """

    def __init__(self, argv, returncode, stderr, timed_out=False):
        reason = "timed out" if timed_out else f"exited with code {returncode}"
        super().__init__(f"ImageMagick command {reason}: {' '.join(argv)}\n{stderr}")
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out


def _frame_suffix(path, frame):
    """Build the ImageMagick frame-index suffix for a source path.

    Multi-frame TIFFs default to frame 0 (the first page) unless a specific
    frame is requested, matching the reference implementation's behavior of
    always selecting the first page of a multi-page TIFF.

    :param str path: Source path.
    :param int|None frame: Explicit frame index, or None to auto-detect.
    :returns: Source path with a '[N]' suffix appended if applicable, else unchanged.
    :rtype: str
    """
    if frame is None and path.lower().endswith((".tif", ".tiff")):
        frame = 0
    return f"{path}[{frame}]" if frame is not None else path


def _run(argv, opts={}):
    """Run an ImageMagick subprocess call safely.

    :param list[str] argv: Full command, e.g. ["convert", ...]. Never shell-interpreted.
    :param dict opts: 'timeout' (float, seconds) overrides CONFIG["image"]["timeout_s"].
    :returns: The completed process' stdout.
    :rtype: str
    :raises ImageMagickError: On non-zero exit or timeout.
    """
    timeout = opts.get("timeout", CONFIG["image"]["timeout_s"])
    try:
        result = subprocess.run(
            argv, shell=False, check=True, timeout=timeout,
            capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as e:
        raise ImageMagickError(argv, e.returncode, e.stderr) from e
    except subprocess.TimeoutExpired as e:
        raise ImageMagickError(argv, None, "", timed_out=True) from e
    return result.stdout


def probe_dimensions(path, opts={}):
    """Probe an image's pixel dimensions via `identify`.

    :param str path: Local image path.
    :param dict opts: 'frame' (int|None) selects a page/frame for multi-frame sources.
        'timeout' (float) overrides the default subprocess timeout.
    :returns: {"width": int, "height": int}
    :rtype: dict
    :raises ImageMagickError: If `identify` fails or times out.
    """
    src = _frame_suffix(path, opts.get("frame"))
    stdout = _run(["identify", "-format", "%wx%h ", src], opts)
    width, height = stdout.strip().split()[0].split("x")
    return {"width": int(width), "height": int(height)}


def thumbnail(src_path, dest_path, width, opts={}):
    """Generate a thumbnail JPEG at the specified width, preserving aspect ratio.

    `convert <src>[frame] -resize {width}x -quality {quality} -alpha remove
    -layers flatten <dest>`.

    :param str src_path: Local source image path.
    :param str dest_path: Local destination path (extension drives output format).
    :param int width: Target width in pixels.
    :param dict opts: 'quality' (int, default CONFIG["image"]["thumbnail_quality"]),
        'frame' (int|None), 'timeout' (float).
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    """
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = [
        "convert", src,
        "-resize", f"{width}x",
        "-quality", str(quality), "-alpha", "remove", "-layers", "flatten",
        dest_path,
    ]
    _run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}


def convert_format(src_path, dest_path, opts={}):
    """Convert an image to a different format via ImageMagick, without resizing.

    `convert <src>[frame] -quality {quality} -alpha remove -layers flatten
    <dest>`. Output format is inferred from dest_path's extension.

    :param str src_path: Local source image path.
    :param str dest_path: Local destination path (extension drives output format).
    :param dict opts: 'quality' (int, default CONFIG["image"]["thumbnail_quality"]),
        'frame' (int|None), 'timeout' (float).
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    """
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = [
        "convert", src,
        "-quality", str(quality), "-alpha", "remove", "-layers", "flatten",
        dest_path,
    ]
    _run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}


def deskew(src_path, dest_path, opts={}):
    """Deskew an image.

    Deskew accuracy degrades on small images, so the source is upsized to a
    working width before running ImageMagick's Hough-transform `-deskew`
    operator, then resized down to the final output width:

    `convert <src>[frame] -resize {working_width}x -deskew {threshold}%
    -quality {quality} -alpha remove -layers flatten -resize {output_width}x
    <dest>`.

    :param str src_path: Local source image path.
    :param str dest_path: Local destination path.
    :param dict opts: 'working_width' (int, default CONFIG["image"]["deskew_working_width"]),
        'threshold' (int, default CONFIG["image"]["deskew_threshold"], percent),
        'output_width' (int, default 1024), 'quality' (int, default
        CONFIG["image"]["thumbnail_quality"]), 'frame' (int|None), 'timeout' (float).
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    """
    working_width = opts.get("working_width", CONFIG["image"]["deskew_working_width"])
    threshold = opts.get("threshold", CONFIG["image"]["deskew_threshold"])
    output_width = opts.get("output_width", 1024)
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = [
        "convert", src,
        "-resize", f"{working_width}x",
        "-deskew", f"{threshold}%",
        "-quality", str(quality),
        "-alpha", "remove",
        "-layers", "flatten",
        "-resize", f"{output_width}x",
        dest_path,
    ]
    _run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}


def prep_ocr(src_path, dest_path, opts={}):
    """Prepare an image for OCR: resize, set DPI, and fuzz-whiten the background.

    `convert <src>[frame] -resize {size}x [-deskew {threshold}%] -density {dpi}
    -alpha remove -fill black -fuzz {fuzz}% +opaque "#FFFFFF" -filter catrom
    -layers flatten -quality {quality} <dest>`. Fuzz-whitening recolors every
    pixel not within `fuzz`% color-distance of white to black, binarizing the
    page to improve OCR contrast.

    :param str src_path: Local source image path.
    :param str dest_path: Local destination path.
    :param dict opts: 'size' (int, default CONFIG["image"]["ocr_prep_size"]), 'dpi'
        (int, default CONFIG["image"]["dpi"]), 'fuzz' (int, default
        CONFIG["image"]["fuzz_threshold"], percent), 'quality' (int, default
        CONFIG["image"]["ocr_prep_quality"]), 'deskew' (bool, default False) —
        whether to include the -deskew step, 'threshold' (int, default
        CONFIG["image"]["deskew_threshold"], only used if deskew=True),
        'frame' (int|None), 'timeout' (float).
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    """
    size = opts.get("size", CONFIG["image"]["ocr_prep_size"])
    dpi = opts.get("dpi", CONFIG["image"]["dpi"])
    fuzz = opts.get("fuzz", CONFIG["image"]["fuzz_threshold"])
    quality = opts.get("quality", CONFIG["image"]["ocr_prep_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = ["convert", src, "-resize", f"{size}x"]
    if opts.get("deskew"):
        threshold = opts.get("threshold", CONFIG["image"]["deskew_threshold"])
        argv += ["-deskew", f"{threshold}%"]
    argv += [
        "-density", str(dpi),
        "-alpha", "remove",
        "-fill", "black",
        "-fuzz", f"{fuzz}%",
        "+opaque", "#FFFFFF",
        "-filter", "catrom",
        "-layers", "flatten",
        "-quality", str(quality),
        dest_path,
    ]
    _run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}
