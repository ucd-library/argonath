# argonath
Digital collections processing pipeline for UC Davis Library. Ingests archival materials into CaskFS, generates image derivatives, runs OCR, produces AI-assisted Schema.org metadata via Samwise/Qwen. Orchestrated by Dagster.

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

### Standalone Install

To run `digtk` as a global command — no `uv run` prefix, no project `.venv` — install
it with [`uv tool install`](https://docs.astral.sh/uv/concepts/tools/) (uv's
pipx-equivalent). This is the sanctioned way to get a bare `digtk` on PATH; it's
different from the ad-hoc pip/pipx install the warning below cautions against,
since it's a deliberate, uv-managed install rather than a stray leftover.

For a released version, install straight from a git tag — no PyPI account or private
package index needed, a pushed tag *is* the release:

```sh
pip install uv   # one-time per machine, if not already present

uv tool install "git+https://github.com/ucd-library/argonath@v0.2.0#subdirectory=exec"

digtk --help   # now available globally
```

For local dev, `uv tool install --editable ./exec` (run from the repo root) does the
same but tracks your local checkout — changes under `exec/digtk/` take effect
immediately, no reinstall.

Either way: `uv tool uninstall digtk` / `uv tool upgrade digtk` / `uv tool list` manage
the install afterward. If `digtk` isn't found on PATH right after installing, run
`uv tool update-shell` once.

Most subcommands talk to an external service and need credentials, set as environment
variables (never as CLI flags — see `digtk.lib.auth`/`digtk.lib.config`):

- **Samwise** (AI chat — `digtk ai`, `digtk metadata`, ...):
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
