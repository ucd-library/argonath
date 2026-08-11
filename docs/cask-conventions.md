# CaskFS Path Conventions

## Root Structure

```
/
├── bronze/         # Raw, immutable, append-only captures
├── silver/         # Derived artifacts (derivatives, OCR, AI metadata, RAG chunks)
├── gold/           # Curated, per-app publish-ready view
└── workspace/      # Per-user staging (formerly user/)
    └── [username]/
        ├── [collection-name]/   # Working collections
        ├── uploads/             # Raw ingest staging
        └── workspace/           # Temp working space
```

Medallion architecture — each layer answers a different question:

- **bronze** — what did we capture, and when? Immutable history.
- **silver** — what did the pipeline derive from it? References bronze binaries; no duplication.
- **gold** — what's currently live in a given app? Flattened, current-state-only, no history.

## Item Identity

Item paths in CaskFS are **arbitrary**. The ARK is stored as:
- The `ark` property in the JSON-LD sidecar
- The `@id` of the JSON-LD document

The path is never parsed to derive the ARK. CaskFS is content-addressed (SHA-256), so
binaries referenced from bronze, silver, or gold are never duplicated on disk — later
layers point at the same underlying blob via `cask://` URIs.

## Bronze Layout

```
bronze/[group]/[collection]/
├── manifest.json                        # structural: collection type, time-format, item-structure hint
├── metadata.jsonld.json                 # domain: schema:Collection
└── [time]/                              # format is per-collection — see Time Dimension below
    ├── manifest.json                    # structural: item list + content hash, run timestamp
    └── box-1/folder-3/photograph.tiff   # item — internal path convention is arbitrary per source
        └── photograph.tiff.jsonld.json  # domain: minimal bronze sidecar
```

`[group]` segments collections by source/use-case for internal organization only
(e.g. `digital-collections`, `website-harvests`, `ae-data`). It is a namespacing
convenience — it carries no meaning downstream in silver or gold.

### Time Dimension

`[time]` format is chosen per collection, not fixed globally:

| Source type | Time format | Notes |
|---|---|---|
| Digital collections | `latest` only | One-shot archival ingest, never re-run — no history needed |
| Website harvests | Date (e.g. `2026-07-22`) | Each crawl is a new immutable snapshot |
| AE data | Year-week (e.g. `2026-W29`) | Matches the source's natural refresh cadence |

Every dated/keyed time-folder is an immutable snapshot — once written, never rewritten.
`latest`'s `manifest.json` lists every current item root plus a content hash per item.
A new harvest/dump run diffs its item list + hashes against the previous `latest`
manifest to find:

- **added** ARKs — register as new partitions, run the pipeline
- **removed** ARKs — no longer present in the batch — delete downstream (see below)
- **changed** ARKs — same ARK, different content hash — reprocess

## Silver Layout

Mirrors the bronze path exactly (`[group]/[collection]/[time]/[item]/`) but holds only
*derived* artifacts — no binaries. Sidecars reference the bronze original via its
`cask://` URI rather than copying it.

```
silver/[group]/[collection]/[time]/[item]/
├── item.jsonld.json            # domain: associatedMedia refs to the derivatives below
├── public-frontend-derivatives/
│   └── images/
│       └── photograph.tiff/    # named after the source's own bronze filename
│           └── images/
│               ├── manifest.json
│               ├── small.jpg
│               ├── medium.jpg
│               └── large.jpg
├── ocr/
│   ├── manifest.json
│   ├── prep.jpg                # OCR-ready image: resized, 300 DPI, fuzz-whitened.
│   │                           # Named prep-deskew.jpg instead if the derivatives
│   │                           # pipeline's OCR-driven orientation check (below)
│   │                           # selected the deskewed candidate.
│   ├── output.hocr
│   └── output.txt
├── metadata/
│   ├── manifest.json
│   ├── ai-metadata.jsonld.json    # AI-generated draft
│   └── human-metadata.jsonld.json # Human input (partial — only touched fields)
└── rag/
    ├── manifest.json
    └── chunks.json                # text chunks + embedding IDs
```

This is the layout for an **image** bronze item. `public-frontend-derivatives/` holds
the small/medium/large product set every image and PDF page publishes for frontend
consumption — see "PDF Pagination" below for how a PDF's pages nest under it
differently than a plain image does. `ocr/` is reused (not media-specific) across
image, PDF, video, and audio items alike — see the PDF/video/audio subsections below
for how each format's own outputs land there, plus the `stream/` and `captions/`
subdirectories video/audio items add. (Video and audio derivatives still use the
older flat `derivatives/` name for now — see their subsections below.)

Every source image gets its own nested directory under `public-frontend-derivatives/
images/`, named after its own bronze filename — this holds regardless of whether the
item has one source image or several, so there's no filename-prefixing scheme to
disambiguate a multi-image item; each source's small/medium/large set and manifest
just live in their own directory instead. `ocr/`, `metadata/`, and `rag/` are
unaffected by this and remain flat, shared directories.

### Scan Orientation

`digtk image derivatives` (the command `derivatives_job` runs) decides an item's
scan orientation once, using a real OCR comparison rather than a fixed rule: it
generates both a deskewed and non-deskewed OCR-prep candidate, runs Tesseract
against each, and keeps whichever has both a higher word count and a higher (or
equal) median confidence. That single decision is then applied consistently to
every thumbnail width and to the published OCR-prep image, so an item is never
right-side-up in one derivative and skewed in another. The decision and the
comparison stats that drove it are recorded in each source's own
`public-frontend-derivatives/images/<filename>/images/manifest.json`'s `params`
(`deskew_selected`, `stats`) for auditability.

This comparison logic lives in `digtk.lib.ocr_utils` and is deliberately shared,
not derivatives-specific: the (separate, later) OCR phase's own `digtk ocr
tesseract` command calls the same underlying Tesseract-invocation primitive
independently to produce `output.hocr`/`output.txt`, with no dependency on
anything the derivatives step did. `digtk.models.digital_website.pdf` reuses the exact
same primitive independently per page (see "PDF Pagination" below).

### Multi-Format Items

An item's bronze binaries can legitimately span more than one media format under one
ARK — real example: an archival transfer supplying **both** a compiled PDF and
per-page TIFF masters for the same physical document, both handed over directly by
the archiving team, neither one derived from the other. `digtk.lib.items.resolve()`
returns every bronze binary regardless of format; each format-specific pipeline
(`digtk.models.digital_website.image`, `.pdf`, `.video`, `.audio`, `.captions`)
filters down to its own subset via
`digtk.models.digital_website.pipeline.filter_binaries_by_format()` and ignores the rest,
so running `digtk image derivatives <ark>` and `digtk pdf derivatives <ark>`
independently against such an item is both safe and the intended way to process it —
**each format gets the full derivative workflow run against it, redundantly**,
matching how the legacy DAMS/Fedora pipeline handled this shape (its
per-Fedora-binary-resource workflow triggering had no cross-binary dedup either).
There's no auto-detecting dispatch command — invoke the relevant per-format commands
directly for a multi-format item; there's currently no Dagster orchestration that
automatically runs every applicable format for one item either, that's out of scope
until it's actually needed.

Each format's `derivatives()`/`generate()` call also merges its own
`argonath:sourceGroup`-tagged entries into the item's `associatedMedia` rather than
replacing the array wholesale, so running the image pipeline and then the PDF
pipeline against the same item leaves both sets of associatedMedia entries in
`item.jsonld.json` afterward — not just the most recently run one.

### PDF Pagination

`digtk pdf derivatives` rasterizes every page of a PDF bronze item (via PyMuPDF,
normalized to a fixed target pixel width regardless of the PDF's native page size),
then runs each rasterized page through the same thumbnail/deskew/OCR-prep pipeline
described above for images — a rasterized page is just a local image file by that
point. Each source PDF gets its own directory under `public-frontend-derivatives/`,
named after its own bronze filename (sibling to `images/`, not nested inside it —
a PDF is a different kind of source than a plain image); within that, each page
publishes into its own plain 1-indexed subdirectory:

```
silver/[group]/[collection]/[time]/[item]/
├── item.jsonld.json                 # associatedMedia: one ImageObject per page, "position": N
├── public-frontend-derivatives/
│   ├── images/                      # sibling to document.pdf/ below — holds plain image sources
│   │   └── ...
│   └── document.pdf/                # named after the source's own bronze filename
│       └── images/
│           ├── manifest.json         # aggregate: page count, per-page orientation summary
│           ├── 1/{manifest.json, small.jpg, medium.jpg, large.jpg}
│           └── 2/...
└── ocr/
    ├── manifest.json
    ├── page-000/{manifest.json, prep.jpg or prep-deskew.jpg}
    └── page-001/...
```

**One ARK covers the whole PDF; pages are never independently ARK-addressable.**
The plain 1-indexed page directory (`1/`, `2/`, ...) is purely an internal
derivative-generation detail — the PDF binary itself remains the source of record
(no page gets an "original" reference the way a plain image derivative would). This
is a deliberately different, lighter-weight model than the "Multi-Part Items"
convention below, which mints a real ARK per part for cases that need independently
citable/searchable pages. Use `parts/` when a document's individual pages need their
own identity; use plain PDF pagination when they don't.

OCR-prep images are unaffected by this restructuring and still publish under the
older zero-padded `ocr/page-NNN` scheme, with their own separate aggregate manifest
at `ocr/manifest.json` — the two aggregate manifests (`.../images/manifest.json` for
the small/medium/large set, `ocr/manifest.json` for OCR-prep) summarize the same
per-page orientation decisions independently, since they're published as separate
`digtk.lib.derivative.write()` calls. A rare multi-PDF item (more than one PDF
sharing an ARK) is disambiguated for free by this scheme — each PDF's own filename
is already its own top-level directory name, so no additional stem-prefixing is
needed there. OCR's flat `page-NNN` naming still nests by source filename stem in
that case: `{stem}/ocr/page-000/...`.

### Deep-Zoom Tiles

`digtk image derivatives`/`digtk pdf derivatives` generate a pyramidal
(multi-resolution), tiled TIFF via ImageMagick's `ptif:` output coder as part of
their standard set — ported byte-for-byte from the reference
dams/services/image-utils pipeline's `generate-image-products.sh`
(`TILED_TIF_OPTS`: 256px tiles, JPEG-compressed, capped at 3072px wide). Intended
for IIIF-style deep-zoom viewing (the legacy system served these through IIPImage
Server + a Leaflet frontend), but **generation only for now** — there's no IIIF
Image API server wired up in argonath yet, so these tiles aren't servable to
anything until that separate infrastructure piece exists.

Publishes as `tiled.tif`, alongside (not in a separate directory from) the
small/medium/large set, sharing that directory's manifest:

```
silver/[group]/[collection]/[time]/[item]/
└── public-frontend-derivatives/
    ├── images/
    │   └── photograph.tiff/
    │       └── images/{manifest.json, small.jpg, medium.jpg, large.jpg, tiled.tif}
    └── document.pdf/
        └── images/1/{manifest.json, small.jpg, medium.jpg, large.jpg, tiled.tif}
```

Runs on the full-resolution source (or, for a PDF, the full-resolution rasterized
page from `digtk.lib.pdf_render`) — not a downscaled derivative, and independent
of that source's own deskew decision — matching the reference pipeline exactly.
`tiled.tif` isn't represented as its own `associatedMedia` entry (it's a TIFF
pyramid, not a flat viewable JPEG like the others) — only the small/medium/large
set gets ImageObject entries.

`digtk.lib.image_magick.tile()` is still available directly for anyone scripting
around this, but there's no standalone `digtk image|pdf tile` command — running
just the tile step alone would overwrite that directory's manifest with one
describing only `tiled.tif`, so regenerating it is a `derivatives()`/`generate()`
re-run.

### Video Streaming and Poster Frames

`digtk video derivatives` transcodes a video bronze item into an HLS
adaptive-bitrate stream (multiple resolution rungs plus a master playlist) and
extracts a poster-frame still. The stream — many small files, not a simple
width-keyed set like image thumbnails — gets its own `stream/` subdirectory,
distinct from `derivatives/` (which holds the poster, a single rendered still, same
conceptual bucket as an image thumbnail):

```
silver/[group]/[collection]/[time]/[item]/
├── item.jsonld.json          # associatedMedia: one VideoObject, "identifier": <stem>
├── derivatives/
│   ├── manifest.json
│   └── poster-1024.jpg
└── stream/
    ├── manifest.json
    ├── playlist.m3u8          # master playlist, always written once every rung is done
    ├── 360p.m3u8 + 360p.mp4
    ├── 480p.m3u8 + 480p.mp4
    ├── 720p.m3u8 + 720p.mp4
    └── 1080p.m3u8 + 1080p.mp4
```

The VideoObject's `identifier` (the source binary's filename stem) is how the same
derivatives() run's caption-transcription step finds the right associatedMedia
entry to attach its caption to (see "Captions/Transcripts" below) — it's not itself
meant as a public identifier.

### Audio Transcoding and Waveforms

`digtk audio derivatives` transcodes an audio bronze item to another format and
renders a waveform image. Both are "a rendered derivative of the source," so they
land in the ordinary `derivatives/` subdirectory, same as image thumbnails and video
posters:

```
silver/[group]/[collection]/[time]/[item]/
├── item.jsonld.json          # associatedMedia: one AudioObject, "identifier": <stem>
└── derivatives/
    ├── manifest.json
    ├── converted.mp3
    └── waveform-1024x256.png
```

### Captions/Transcripts

`digtk video derivatives`/`digtk audio derivatives` run local Whisper ASR against
every source video/audio file as part of their standard derivative set —
`digtk.models.digital_website.captions.transcribe_sources()` is called directly
using the sources already staged for HLS/poster or transcode/waveform generation
(no separate staging pass), producing a WebVTT caption file per source. Shared by
both media types (Whisper pulls audio from a video file itself via ffmpeg
internally), captions land in their own `captions/` subdirectory:

```
silver/[group]/[collection]/[time]/[item]/
└── captions/
    ├── manifest.json
    └── output-en.vtt
```

Machine-generated and unreviewed: the caption gets merged into the matching
VideoObject/AudioObject associatedMedia entry (matched by the `identifier` stem —
see above, written by the same derivatives() run just before the caption step) as
a nested `caption` MediaObject, and the item's `argonath:fieldSources` map records
`"caption": "ai"` (see Field Sources below) so downstream review workflows know it
needs a human pass. This merge is additive — unlike every other derivatives step,
which regenerates and replaces its associatedMedia entries each run, caption
generation only adds to whatever entry already exists.

There's no standalone `digtk captions transcribe` command — captions are always
generated as part of `derivatives()`, controllable via that command's
`--language`/`--model` flags. Image and PDF items have no equivalent — captions
don't apply to a still image or an unrasterized document page.

## Gold Layout

Curated, per-app, **current-state only** — no `[time]`, no `[group]`. A single
bronze/silver collection can be promoted into more than one gold app at once
(e.g. `digital-dev` and `digital-prod` at different review states, plus `arda` as
the cross-collection aggregate) — promotion is per-app, not per-source, so gold
dimensions by app instead of by source grouping.

```
gold/[app]/[collection]/[item]/
├── manifest.json          # structural: promotion status + provenance (source group/time promoted from)
└── item.jsonld.json       # domain: final merged sidecar (argonath:fieldSources)
```

`app` is one of `arda` (global cross-collection RAG aggregate — every collection
ends up here), `digital-dev`, `digital-prod`, or future app targets. Because gold
has no time dimension, its `manifest.json` is the only record of which bronze/silver
snapshot a given entry was promoted from — promotion, not reprocessing, is what
updates a gold entry.

## Multi-Part Items

Multi-part items (books, documents) follow the same pattern under a `parts/`
subdirectory — each part (e.g. a page) is its own item with its own ARK, sidecar,
and process directories, linked to the parent via `hasPart`/`isPartOf`.

The same convention covers hierarchical web content: a crawled page is an item
like any other, linked to its site's collection ARK via `isPartOf`. No separate
modeling is needed for website hierarchy versus multi-part archival items.

**Not the same thing as PDF pagination** (see "PDF Pagination" above): a PDF
bronze item's pages are `public-frontend-derivatives/<pdf-filename>/images/<n>`/
`ocr/page-NNN` subdirectories under one ARK, with no per-page ARK and no `parts/` —
a lighter-weight model for pages that don't need independent identity. Use this
`parts/` convention instead when individual pages/parts genuinely need their own
ARK (independently citable, searchable, or RAG-indexable).

## Structural vs. Domain Metadata

Every level (collection, time-folder, item, process directory) keeps exactly two
kinds of file, never merged into one:

| File | Content | Never contains |
|---|---|---|
| `manifest.json` | Structural/system: CaskFS layout facts, pipeline run records, item lists + hashes, promotion status | schema.org / descriptive fields |
| `metadata.jsonld.json` / `<item>.jsonld.json` | Domain: schema.org descriptive metadata about the collection or item | Pipeline state, run history, promotion status |
| `silver/.../[item]/item.jsonld.json` | Domain: `schema:associatedMedia` `ImageObject` refs to that item's derivatives, referencing the bronze original via `cask://` rather than copying it | Pipeline state, run history — and never the bronze sidecar's own fields, since bronze is immutable/append-only and this file lives in silver instead of amending it |

This is a deliberate reaction against LDP's convention of mixing container/structural
triples with a resource's own descriptive triples in one graph — CaskFS keeps them as
physically separate files at every level instead.

## Sidecar Convention

Every binary has a sibling `.jsonld.json` sidecar:

```json
{
  "@context": {
    "@vocab": "https://schema.org/",
    "argonath": "https://lib.ucdavis.edu/argonath/ns#"
  },
  "@id": "ark:/87287/d7abc1234",
  "ark": "ark:/87287/d7abc1234",
  "@type": "Photograph",
  "name": "...",
  "identifier": [
    { "@type": "PropertyValue", "propertyID": "ARK",    "value": "ark:/87287/d7abc1234" },
    { "@type": "PropertyValue", "propertyID": "box",    "value": "1" },
    { "@type": "PropertyValue", "propertyID": "folder", "value": "3" }
  ],
  "argonath:fieldSources": {
    "name": "human",
    "description": "ai",
    "dateCreated": "hybrid",
    "subject": "ai-approved"
  }
}
```

### Field Sources

The `argonath:fieldSources` map records per-field provenance:

| Value | Meaning |
|---|---|
| `ai` | AI generated, not yet reviewed |
| `human` | Fully human-authored |
| `ai-approved` | AI draft accepted as-is by a human |
| `hybrid` | AI draft edited by a human |

## manifest.json

Each structural file records the run/state relevant to its level — for a process
directory:

```json
{
  "written_at": "2026-06-24T10:00:00Z",
  "status": "success",
  "command": ["digtk", "image", "thumbnail", "ark:/87287/d7abc1234", "--widths", "256,512,1024"],
  "params": { "widths": [256, 512, 1024] },
  "outputs": ["small.jpg", "medium.jpg", "large.jpg"],
  "duration_s": 1.4
}
```

Manifests persist beyond Dagster's run retention window and are the durable
record of what was run, when, and what it produced.

## ARK Format

- NAAN: `87287` (UC Davis Library)
- Shoulder: `d7`
- Format: `ark:/87287/d7[noid]`
- NOID check-digit algorithm applied at mint time
