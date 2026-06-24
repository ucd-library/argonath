# CaskFS Path Conventions

## Root Structure

```
/
├── app/
│   ├── arda/           # Global aggregate — all collections · sole RAG input
│   ├── digital-dev/    # Development app space
│   └── digital-prod/   # Production app space
└── user/
    └── [username]/
        ├── [collection-name]/   # Working collections
        ├── uploads/
        └── workspace/
```

Additional app spaces may be added. The three above are the known targets.

## Item Identity

Item paths in CaskFS are **arbitrary**. The ARK is stored as:
- The `ark` property in the JSON-LD sidecar
- The `@id` of the JSON-LD document

The path is never parsed to derive the ARK. Box/folder archival paths are
used as a convention in examples but any path is valid.

## Collection Layout

```
[space]/[collection-name]/
├── metadata.jsonld.json                       # schema:Collection

├── box-1/folder-3/photograph.tiff             # original binary (path = arbitrary)
├── box-1/folder-3/photograph.tiff.jsonld.json # canonical merged sidecar
└── box-1/folder-3/photograph.tiff/            # process directories
    ├── derivatives/
    │   ├── manifest.json
    │   ├── thumb-256.jpg
    │   ├── thumb-512.jpg
    │   └── web-1024.jpg
    ├── ocr/
    │   ├── manifest.json
    │   ├── prep.tiff            # deskewed + contrast-normalized intermediate
    │   ├── output.hocr
    │   └── output.txt
    ├── metadata/
    │   ├── manifest.json
    │   ├── ai-metadata.jsonld.json
    │   └── human-metadata.jsonld.json
    └── rag/
        ├── manifest.json
        └── chunks.json
```

Multi-part items (books, documents) follow the same pattern under a `parts/` subdirectory.

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

Each process directory contains a `manifest.json` recording the run:

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
