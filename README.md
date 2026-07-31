# argonath
Digital collections processing pipeline for UC Davis Library. Ingests archival materials into CaskFS, generates image derivatives, runs OCR, produces AI-assisted Schema.org metadata via Samwise/Qwen, and indexes for RAG search using LlamaIndex + pgvector. Orchestrated by Dagster.

For local dev (docker compose) and prod (libk8s) deployment, see
[argonath-deployment](https://github.com/ucd-library/argonath-deployment).

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
