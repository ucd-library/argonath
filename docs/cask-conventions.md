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
├── derivatives/
│   ├── manifest.json
│   ├── thumb-256.jpg
│   ├── thumb-512.jpg
│   └── web-1024.jpg
├── ocr/
│   ├── manifest.json
│   ├── prep.tiff              # intermediate: deskewed + contrast-normalized
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

## Structural vs. Domain Metadata

Every level (collection, time-folder, item, process directory) keeps exactly two
kinds of file, never merged into one:

| File | Content | Never contains |
|---|---|---|
| `manifest.json` | Structural/system: CaskFS layout facts, pipeline run records, item lists + hashes, promotion status | schema.org / descriptive fields |
| `metadata.jsonld.json` / `<item>.jsonld.json` | Domain: schema.org descriptive metadata about the collection or item | Pipeline state, run history, promotion status |

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
  "command": ["dc", "image", "thumbnail", "ark:/87287/d7abc1234", "--widths", "256,512,1024"],
  "params": { "widths": [256, 512, 1024] },
  "outputs": ["thumb-256.jpg", "thumb-512.jpg", "web-1024.jpg"],
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
