# argonath
Digital collections processing pipeline for UC Davis Library. Ingests archival materials into CaskFS, generates image derivatives, runs OCR, produces AI-assisted Schema.org metadata via Samwise/Qwen, and indexes for RAG search using LlamaIndex + pgvector. Orchestrated by Dagster.

For local dev (docker compose) and prod (libk8s) deployment, see
[argonath-deployment](https://github.com/ucd-library/argonath-deployment).

## Overview

Argonath runs on top of **project-anduin** — it is not a standalone deployment.
Anduin provides the shared Postgres/pgvector instance and the Dagster deployment
pattern (CeleryExecutor, `exec()`-bridge asset shape) that this repo's `dagster/`
definitions follow. When diagnosing infra issues (DB connectivity, Dagster
daemon/webserver health, pgvector extension state), check project-anduin first —
argonath does not provision or own that infrastructure itself.

### Digitization AI

Per-item OCR and metadata generation run as Dagster assets that shell out to the
`digtk` CLI. Tesseract produces the baseline OCR text, with an ML fallback (surya/docTR)
for low-confidence pages. Schema.org metadata is drafted by Samwise (self-hosted,
OpenAI-compatible API, Qwen models) from the item's image and OCR text, then merged
into the canonical sidecar alongside any human-authored fields. All Samwise calls go
through `digtk.lib.ai` — see `digtk config show` / `digtk ai --help` for model and
endpoint configuration.

### RAG pipeline

`digtk search index|query` (LlamaIndex + pgvector) chunks and embeds item text via
Samwise's embedding model and stores vectors in project-anduin's Postgres, scoped by
`--space` and queried with ranked, ARK-referenced results. Indexing is triggered by
the `index_for_rag` Dagster asset and can also be run ad hoc through the ops CLI.

## Testing

The `digtk` CLI (`exec/`) is tested with pytest.

```sh
cd exec
python3 -m venv .venv && source .venv/bin/activate   # first time only
pip install -e ".[dev]"                              # first time only

python3 -m pytest                    # run the full suite
python3 -m pytest tests/test_ai.py   # run one file
python3 -m pytest -k get_client       # run tests matching a name
```

Coverage reports print automatically after each run (configured in `exec/pyproject.toml`).
Tests live in `exec/tests/`, one file per `digtk.lib` module (e.g. `test_ai.py` covers
`digtk/lib/ai.py`).
