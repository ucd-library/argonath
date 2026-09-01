"""
ImageMagick subprocess wrapper.

All operations shell out to ImageMagick's `convert`/`identify` binaries via
subprocess, using list-args (never shell=True) and check=True, so a failed
step raises immediately instead of silently letting a multi-step recipe
continue against a stale or missing file — see the phase 0.4 migration notes
for the production bug class (from the reference dams/services/image-utils
implementation) this is deliberately fixing, not replicating.

This module is ARK/CaskFS-agnostic: every function takes and returns local
filesystem paths only. digtk.models.digital_website.image is responsible for
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


def run(argv, opts={}):
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


def _use_magick(opts):
    """Whether this call should use ImageMagick 7's unified `magick` CLI instead
    of the legacy `convert`/`identify` binaries.

    :param dict opts: 'magick' (bool) overrides CONFIG["image"]["use_magick"].
    :rtype: bool
    """
    return bool(opts.get("magick", CONFIG["image"]["use_magick"]))


def convert_argv(opts):
    """Resolve the base command for a convert-style invocation.

    Uses `magick` (IMv7's unified CLI, which convert-style operators run under
    directly) when requested — see _use_magick() — else the legacy `convert`
    binary, named "convert" by default but overridable via
    CONFIG["image"]["convert_bin"] (DIGTK_IMAGE_CONVERT_BIN) for installs where
    it's named or aliased differently.

    :param dict opts: See _use_magick().
    :returns: Argv prefix, e.g. ["magick"] or ["convert"].
    :rtype: list[str]
    """
    return ["magick"] if _use_magick(opts) else [CONFIG["image"]["convert_bin"]]


def _identify_argv(opts):
    """Resolve the base command for an identify-style invocation.

    Mirrors convert_argv(): `magick identify` under IMv7's unified CLI, else
    the legacy `identify` binary (CONFIG["image"]["identify_bin"] /
    DIGTK_IMAGE_IDENTIFY_BIN).

    :param dict opts: See _use_magick().
    :returns: Argv prefix, e.g. ["magick", "identify"] or ["identify"].
    :rtype: list[str]
    """
    return ["magick", "identify"] if _use_magick(opts) else [CONFIG["image"]["identify_bin"]]


def probe_dimensions(path, opts={}):
    """Probe an image's pixel dimensions via `identify`.

    :param str path: Local image path.
    :param dict opts: 'frame' (int|None) selects a page/frame for multi-frame sources.
        'timeout' (float) overrides the default subprocess timeout. 'magick' (bool)
        uses ImageMagick 7's unified CLI instead of the legacy `identify` binary —
        see _use_magick().
    :returns: {"width": int, "height": int}
    :rtype: dict
    :raises ImageMagickError: If `identify` fails or times out.
    """
    src = _frame_suffix(path, opts.get("frame"))
    stdout = run(_identify_argv(opts) + ["-format", "%wx%h ", src], opts)
    width, height = stdout.strip().split()[0].split("x")
    return {"width": int(width), "height": int(height)}


def probe_all_frame_dimensions(path, opts={}):
    """Probe the pixel dimensions of every frame in a multi-frame image, via `identify`.

    Unlike probe_dimensions(), this never selects a single frame — `identify`
    applies -format once per frame when none is selected, which is exactly what a
    pyramidal TIFF's resolution-level enumeration needs (see
    digtk.models.digital_website.image's rich manifest content: tile()'s output has
    one frame per pyramid level, largest first).

    :param str path: Local image path.
    :param dict opts: 'timeout' (float), 'magick' (bool) — see _use_magick().
    :returns: One {"width": int, "height": int} entry per frame, in file order.
    :rtype: list[dict]
    :raises ImageMagickError: If `identify` fails or times out.
    """
    stdout = run(_identify_argv(opts) + ["-format", "%wx%h\n", path], opts)
    dims = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        width, height = line.split("x")
        dims.append({"width": int(width), "height": int(height)})
    return dims


def _bit_depth(path, opts={}):
    """Read an image's bits-per-channel via `identify`.

    Defaults to 8 if the probe itself fails, rather than raising — the
    subsequent `convert` call will surface any real error against the source.

    :param str path: Local image path.
    :param dict opts: 'frame' (int|None), 'timeout' (float) — see probe_dimensions().
    :returns: Bit depth of the selected frame.
    :rtype: int
    """
    src = _frame_suffix(path, opts.get("frame"))
    try:
        stdout = run(_identify_argv(opts) + ["-format", "%[bit-depth]", src], opts)
    except ImageMagickError:
        return 8
    try:
        return int(stdout.strip())
    except ValueError:
        return 8


def thumbnail(src_path, dest_path, width, opts={}):
    """Generate a thumbnail at the specified width, preserving aspect ratio.

    Applies EXIF orientation and auto-levels sources deeper than 8 bits per
    channel — 16-bit archival TIFF masters render flat/dark without this (a
    linear 16->8 scale leaves scans that only use a narrow band of the tonal
    range looking washed out, so the tonal range is stretched to full scale
    first; same for float/32-bit sources). Never upscales a source narrower
    than the target width.

    `convert <src>[frame] -auto-orient [-auto-level] -resize {width}x>
    -quality {quality} -alpha remove -layers flatten <dest>`.

    :param str src_path: Local source image path.
    :param str dest_path: Local destination path (extension drives output format).
    :param int width: Target width in pixels.
    :param dict opts: 'quality' (int, default CONFIG["image"]["thumbnail_quality"]),
        'frame' (int|None), 'timeout' (float), 'magick' (bool) — see _use_magick().
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    """
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = convert_argv(opts) + [src, "-auto-orient"]
    if _bit_depth(src_path, opts) > 8:
        argv.append("-auto-level")
    argv += [
        "-resize", f"{width}x>",
        "-quality", str(quality), "-alpha", "remove", "-layers", "flatten",
        dest_path,
    ]
    run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}


def convert_format(src_path, dest_path, opts={}):
    """Convert an image to a different format via ImageMagick, without resizing.

    `convert <src>[frame] -quality {quality} -alpha remove -layers flatten
    <dest>`. Output format is inferred from dest_path's extension.

    :param str src_path: Local source image path.
    :param str dest_path: Local destination path (extension drives output format).
    :param dict opts: 'quality' (int, default CONFIG["image"]["thumbnail_quality"]),
        'frame' (int|None), 'timeout' (float), 'magick' (bool) — see _use_magick().
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    """
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = convert_argv(opts) + [
        src,
        "-quality", str(quality), "-alpha", "remove", "-layers", "flatten",
        dest_path,
    ]
    run(argv, opts)
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
        CONFIG["image"]["thumbnail_quality"]), 'frame' (int|None), 'timeout' (float),
        'magick' (bool) — see _use_magick().
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    """
    working_width = opts.get("working_width", CONFIG["image"]["deskew_working_width"])
    threshold = opts.get("threshold", CONFIG["image"]["deskew_threshold"])
    output_width = opts.get("output_width", 1024)
    quality = opts.get("quality", CONFIG["image"]["thumbnail_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = convert_argv(opts) + [
        src,
        "-resize", f"{working_width}x",
        "-deskew", f"{threshold}%",
        "-quality", str(quality),
        "-alpha", "remove",
        "-layers", "flatten",
        "-resize", f"{output_width}x",
        dest_path,
    ]
    run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}


def detect_skew_angle(src_path, opts={}):
    """Measure the skew angle ImageMagick's `-deskew` operator would correct.

    `-deskew` sets a `deskew:angle` image artifact describing the detected
    correction angle, but that artifact only exists within the convert
    invocation that ran `-deskew` -- it is not preserved in a saved output
    file's own metadata (confirmed: reading `%[deskew:angle]` back via a
    separate `identify` call on an already-deskewed file raises "unknown
    image property"). So this always issues its own dedicated convert call
    against an `info:` pseudo-destination (no real image is rendered/written)
    rather than trying to read the angle off deskew()'s or prep_ocr()'s output:

    `convert <src>[frame] -deskew {threshold}% -format "%[deskew:angle]" info:`

    :param str src_path: Local source image path.
    :param dict opts: 'threshold' (int, default CONFIG["image"]["deskew_threshold"],
        percent), 'frame' (int|None), 'timeout' (float), 'magick' (bool) — see
        _use_magick().
    :returns: Detected skew angle in degrees (the sign/direction follows
        ImageMagick's own convention; callers comparing against a threshold
        should compare the absolute value).
    :rtype: float
    :raises ImageMagickError: If `convert` fails or times out.
    """
    threshold = opts.get("threshold", CONFIG["image"]["deskew_threshold"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = convert_argv(opts) + [
        src, "-deskew", f"{threshold}%", "-format", "%[deskew:angle]", "info:",
    ]
    stdout = run(argv, opts)
    return float(stdout.strip())


def prep_ocr(src_path, dest_path, opts={}):
    """Prepare an image for OCR: resize, set DPI, and fuzz-whiten the background.

    Applies EXIF orientation and auto-levels sources deeper than 8 bits per
    channel, same as thumbnail(). Fuzz-whitening recolors every pixel not
    within `fuzz`% color-distance of white to black, binarizing the page to
    improve OCR contrast.

    `convert <src>[frame] -auto-orient [-auto-level] [enhance flags] -resize
    {size}x [-deskew {threshold}%] -density {dpi} -alpha remove -fill black
    -fuzz {fuzz}% +opaque "#FFFFFF" -filter catrom -layers flatten -quality
    {quality} <dest>`.

    :param str src_path: Local source image path.
    :param str dest_path: Local destination path.
    :param dict opts: 'size' (int, default CONFIG["image"]["ocr_prep_size"]), 'dpi'
        (int, default CONFIG["image"]["dpi"]), 'fuzz' (int, default
        CONFIG["image"]["fuzz_threshold"], percent), 'quality' (int, default
        CONFIG["image"]["ocr_prep_quality"]), 'deskew' (bool, default False) —
        whether to include the -deskew step, 'threshold' (int, default
        CONFIG["image"]["deskew_threshold"], only used if deskew=True),
        'enhance' (None|'auto'|'gray', default None) — extra contrast
        enhancement for faded originals (hectograph/mimeo print, pale
        typescript): 'auto' stretches contrast keeping color, 'gray' also
        converts to grayscale for maximum OCR legibility, 'frame' (int|None),
        'timeout' (float), 'magick' (bool) — see _use_magick().
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    :raises ValueError: On an unknown 'enhance' mode.
    """
    size = opts.get("size", CONFIG["image"]["ocr_prep_size"])
    dpi = opts.get("dpi", CONFIG["image"]["dpi"])
    fuzz = opts.get("fuzz", CONFIG["image"]["fuzz_threshold"])
    quality = opts.get("quality", CONFIG["image"]["ocr_prep_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = convert_argv(opts) + [src, "-auto-orient"]
    if _bit_depth(src_path, opts) > 8:
        argv.append("-auto-level")
    enhance = opts.get("enhance")
    if enhance == "gray":
        argv += ["-colorspace", "Gray", "-contrast-stretch", "2%x2%"]
    elif enhance == "auto":
        argv += ["-contrast-stretch", "2%x2%"]
    elif enhance:
        raise ValueError(f"unknown enhance mode: {enhance!r} (use 'auto' or 'gray')")

    argv += ["-resize", f"{size}x"]
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
    run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}


def tile(src_path, dest_path, opts={}):
    """Generate a pyramidal (multi-resolution), tiled TIFF via ImageMagick's `ptif:`
    output coder, for IIIF-style deep-zoom serving.

    Ported byte-for-byte from the reference dams/services/image-utils pipeline's
    generate-image-products.sh TILED_TIF_OPTS — same flags, same order:

    `convert <src>[frame] -define tiff:tile-geometry={tile_size}x{tile_size}
    -compress JPEG -alpha remove -resize {max_width}x -quality {quality}
    -layers flatten ptif:<dest>`.

    Generation only — there's no IIIF Image API server wired up yet to serve these
    tiles (see docs/cask-conventions.md's "Deep-Zoom Tiles" section), so this isn't
    part of thumbnail()/generate()'s standard derivative set.

    :param str src_path: Local source image path (or a rasterized PDF page).
    :param str dest_path: Local destination path — a plain .tif path; the `ptif:`
        coder prefix is applied internally, callers don't need to know about it.
    :param dict opts: 'tile_size' (int, default CONFIG["image"]["tile_size"]),
        'max_width' (int, default CONFIG["image"]["tile_max_width"]), 'quality' (int,
        default CONFIG["image"]["tile_quality"]), 'frame' (int|None), 'timeout' (float),
        'magick' (bool) — see _use_magick().
    :returns: {"argv": list[str], "dest_path": str}
    :rtype: dict
    :raises ImageMagickError: If `convert` fails or times out.
    """
    tile_size = opts.get("tile_size", CONFIG["image"]["tile_size"])
    max_width = opts.get("max_width", CONFIG["image"]["tile_max_width"])
    quality = opts.get("quality", CONFIG["image"]["tile_quality"])
    src = _frame_suffix(src_path, opts.get("frame"))

    argv = convert_argv(opts) + [
        src,
        "-define", f"tiff:tile-geometry={tile_size}x{tile_size}",
        "-compress", "JPEG",
        "-alpha", "remove",
        "-resize", f"{max_width}x",
        "-quality", str(quality),
        "-layers", "flatten",
        f"ptif:{dest_path}",
    ]
    run(argv, opts)
    return {"argv": argv, "dest_path": dest_path}
