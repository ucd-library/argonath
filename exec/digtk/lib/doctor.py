"""
Checks whether the external system binaries digtk's pipeline commands shell out to
are installed and on PATH: tesseract, ImageMagick, Ghostscript, poppler's pdfinfo,
ffmpeg, veraPDF (plus the Java runtime it needs), and the openai-whisper CLI.

Domain-agnostic the same way digtk.lib.whisper_utils/digtk.lib.image_magick are: this
only resolves binary names (from digtk.lib.config.CONFIG, the same source the modules
that actually shell out to them read) and checks they're runnable — it doesn't know
what digtk *does* with any of them. See digtk.commands.doctor for the `digtk doctor
check` command that reports this to the user.
"""
import shutil
import subprocess

from digtk.lib.config import CONFIG

# One entry per external tool `digtk doctor check` reports on. 'bin' is resolved the
# same way the lib module that actually shells out to it resolves it (see 'used_by'),
# so a config override (e.g. DIGTK_IMAGE_CONVERT_BIN) is reflected here too.
# 'version_flag' is only used for the best-effort version string in the report, never
# to decide availability — poppler/ImageMagick/ffmpeg/etc. don't agree on one flag
# convention, so a wrong guess there must not turn a working install into "missing".
# 'install_url' points at the tool's own install docs, not a specific package manager,
# since the right install command differs by OS/distro.
TOOLS = [
    {
        "name": "tesseract", "bin": "tesseract", "version_flag": "--version",
        "used_by": "digtk.lib.ocr_utils (digtk ocr)",
        "install_url": "https://tesseract-ocr.github.io/tessdoc/Installation.html",
    },
    {
        "name": "imagemagick", "bin": CONFIG["image"]["convert_bin"], "version_flag": "-version",
        "used_by": "digtk.lib.image_magick (digtk image, digtk showcase image/pdf)",
        "install_url": "https://imagemagick.org/script/download.php",
    },
    {
        "name": "ghostscript", "bin": CONFIG["accessibility"]["gs_bin"], "version_flag": "--version",
        "used_by": "ImageMagick's PDF delegate, digtk.models.pdf_tag (digtk accessibility tag-pdf)",
        "install_url": "https://www.ghostscript.com/releases/gsdnld.html",
    },
    {
        "name": "poppler (pdfinfo)", "bin": CONFIG["pdf"]["pdfinfo_bin"], "version_flag": "-v",
        "used_by": "digtk.lib.pdf_render (digtk showcase pdf)",
        "install_url": "https://poppler.freedesktop.org/",
    },
    {
        "name": "ffmpeg", "bin": CONFIG["video"]["ffmpeg_bin"], "version_flag": "-version",
        "used_by": "digtk.lib.ffmpeg_utils (digtk showcase video/audio)",
        "install_url": "https://ffmpeg.org/download.html",
    },
    {
        "name": "verapdf", "bin": CONFIG["accessibility"]["verapdf_bin"], "version_flag": "--version",
        "used_by": "digtk.models.pdf_tag (digtk accessibility tag-pdf, unless --no-validate)",
        "install_url": "https://software.verapdf.org/",
    },
    {
        "name": "java", "bin": "java", "version_flag": "-version",
        "used_by": "verapdf's own runtime requirement",
        "install_url": "https://adoptium.net/",
    },
    {
        "name": "whisper", "bin": CONFIG["captions"]["whisper_bin"], "version_flag": "--version",
        "used_by": "digtk.lib.whisper_utils (digtk showcase video/audio captions)",
        "install_url": "https://github.com/openai/whisper#setup",
    },
]


def check_tool(tool, opts={}):
    """Check whether one external tool (see TOOLS) is installed and on PATH.

    Availability is decided by shutil.which() alone; 'version_flag' only drives a
    best-effort version string for the report, so a tool-specific flag digtk didn't
    anticipate on some installed version can't turn a present binary into "missing".
    If the version call itself exits non-zero, its first output line is reported as
    'warning' instead of 'version' — seen in practice on macOS, where a `which`-able
    `java` is really Apple's stub that just prints a "no JRE" prompt message, and a
    `which`-able `ffmpeg` can still fail at runtime on a missing shared library — a
    present-on-PATH binary that doesn't actually run shouldn't be reported as if its
    error output were a real version string.

    :param dict tool: One TOOLS entry.
    :param dict opts: 'timeout' (float, default 10) for the version-string subprocess call.
    :returns: {"name", "bin", "available" (bool), "path" (str|None), "version" (str|None),
        "warning" (str|None), "used_by", "install_url"}.
    :rtype: dict
    """
    path = shutil.which(tool["bin"])
    version = None
    warning = None
    if path is not None:
        try:
            proc = subprocess.run(
                [tool["bin"], tool["version_flag"]], capture_output=True, text=True,
                timeout=opts.get("timeout", 10),
            )
            first_line = (proc.stdout or proc.stderr).strip().splitlines()
            first_line = first_line[0] if first_line else None
            if proc.returncode == 0:
                version = first_line
            else:
                warning = first_line
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {
        "name": tool["name"], "bin": tool["bin"], "available": path is not None,
        "path": path, "version": version, "warning": warning,
        "used_by": tool["used_by"], "install_url": tool["install_url"],
    }


def check_all(opts={}):
    """Check every tool in TOOLS.

    :param dict opts: Forwarded to check_tool().
    :returns: One check_tool() result per TOOLS entry, in TOOLS order.
    :rtype: list[dict]
    """
    return [check_tool(tool, opts) for tool in TOOLS]
