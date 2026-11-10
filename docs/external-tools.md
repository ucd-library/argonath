# External tool requirements

`digtk` shells out to several external CLI tools rather than reimplementing image/PDF/
video processing itself. They're only needed if you run `digtk` directly on your own
machine — the worker Docker image (see root `Dockerfile`) already has all of them.
Run `digtk doctor check` to see which of these are installed/missing on your machine.

| Tool | Used for | Official install instructions |
| --- | --- | --- |
| [Tesseract](https://tesseract-ocr.github.io/) | `digtk ocr` — baseline OCR text | [tesseract-ocr.github.io/tessdoc/Installation.html](https://tesseract-ocr.github.io/tessdoc/Installation.html) |
| [ImageMagick](https://imagemagick.org/) | `digtk image`, `digtk showcase image/pdf` — thumbnails, conversion, deskew, OCR prep | [imagemagick.org/script/download.php](https://imagemagick.org/script/download.php) |
| [Ghostscript](https://www.ghostscript.com/) | ImageMagick's PDF rendering delegate; `digtk accessibility tag-pdf`'s font embedding | [ghostscript.com/releases/gsdnld.html](https://www.ghostscript.com/releases/gsdnld.html) |
| [Poppler](https://poppler.freedesktop.org/) (`pdfinfo`) | `digtk showcase pdf` — page count/size lookup before rasterizing | [poppler.freedesktop.org](https://poppler.freedesktop.org/) |
| [FFmpeg](https://ffmpeg.org/) | `digtk showcase video`/`audio` — transcode, HLS, waveform | [ffmpeg.org/download.html](https://ffmpeg.org/download.html) |
| [veraPDF](https://verapdf.org/) | `digtk accessibility tag-pdf` — PDF/UA-1 validation (skip with `--no-validate`) | [software.verapdf.org](https://software.verapdf.org/) |
| Java (JRE) | Required by veraPDF | [adoptium.net](https://adoptium.net/) |
| [OpenAI Whisper](https://github.com/openai/whisper) | `digtk showcase video`/`audio` captions (local speech-to-text) | [github.com/openai/whisper#setup](https://github.com/openai/whisper#setup) |

## Quick install (macOS, via Homebrew)

```sh
brew install tesseract imagemagick ghostscript poppler ffmpeg
brew install --cask temurin   # Java, for verapdf
pip install -U openai-whisper
```

veraPDF has no Homebrew formula — download the installer from
[software.verapdf.org](https://software.verapdf.org/) directly.

## Quick install (Debian/Ubuntu, via apt)

Matches what the worker `Dockerfile` installs:

```sh
sudo apt-get install -y tesseract-ocr tesseract-ocr-eng imagemagick ghostscript \
  poppler-utils ffmpeg default-jre-headless
pip install -U openai-whisper
```

veraPDF has no apt package either — see the `Dockerfile` for how the worker image
installs it from veraPDF's own release archive, or follow
[software.verapdf.org](https://software.verapdf.org/)'s own instructions.
