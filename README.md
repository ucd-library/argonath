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

## Local Development

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/getting-started/installation/)
(`pip install uv` if you don't have it) — the same tool the Docker image installs with
(see `Dockerfile`), so local dependency resolution stays consistent with the built image.

```sh
cd exec
uv venv .venv                # first time only
uv pip install -e ".[dev]"   # first time only; auto-detects .venv above, no activation
                              # needed; the [dev] extra pulls in pytest for Testing below

uv run digtk --help   # confirm it works
```

`-e` (editable) means changes under `digtk/` take effect immediately, no reinstall needed.

Use `uv run digtk ...` (not a bare `digtk ...`) unless you've `source .venv/bin/activate`d —
`uv run` always runs *this* project's venv install regardless of what else is on PATH.
A bare `digtk` depends on PATH order and will silently run a different install if one
exists elsewhere (e.g. a global pipx/pip install) — if that happens you'll see an error
like `ModuleNotFoundError: No module named 'bs4'` from a stale copy missing a dependency
this project added later. `which digtk` should point inside `exec/.venv/bin/` — if it
points anywhere else, that's the shadowing.

Most subcommands talk to an external service and need credentials, set as environment
variables (never as CLI flags — see `digtk.lib.auth`/`digtk.lib.config`):

- **Samwise** (AI chat/embeddings — `digtk ai`, `digtk metadata`, ...):
  `export SAMWISE_API_KEY=...` (`SAMWISE_API_URL` too, if not using the default).
- **CaskFS** (file storage — `digtk ingest`, `digtk collection`, any `cask:/`-prefixed path):
  `digtk auth login` opens a browser to log in via the Anduin auth gateway and stores
  the result in `~/.digtk/credentials.json` (or set `CASK_API_URL`/`CASK_API_KEY`
  directly — see `digtk auth whoami` to check what's currently resolved).
- **Preservica** (`digtk preservica import|tree`): `export PRESERVICA_TENANT=...
  PRESERVICA_USERNAME=... PRESERVICA_PASSWORD=...` (`PRESERVICA_BASE_URL` too, if the
  tenant-derived default doesn't apply) — see `digtk.lib.auth.resolve_preservica()`.

`digtk config show` prints every setting's current resolved value; `digtk config set
<dotpath> [value]` persists a non-secret default to `~/.digtk/config.json` (omit `value`
to be prompted, with hidden input for secrets) so you don't need to re-export env vars
every session.

## Testing

The `digtk` CLI (`exec/`) is tested with pytest — the `uv pip install -e ".[dev]"` step
above already includes it.

```sh
cd exec
uv run pytest                    # run the full suite
uv run pytest tests/test_ai.py   # run one file
uv run pytest -k get_client       # run tests matching a name
```

Coverage reports print automatically after each run (configured in `exec/pyproject.toml`).
Tests live in `exec/tests/`, one file per `digtk.lib` module (e.g. `test_ai.py` covers
`digtk/lib/ai.py`).
