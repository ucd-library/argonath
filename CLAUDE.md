# argonath

## Core Libraries

These are the load-bearing libs for pipeline code. When building a new command or
pipeline step, use them — extend one only if it genuinely doesn't fit, don't route
around it by calling a lower-level API directly.

- **`digtk.lib.fs`** (`exec/digtk/lib/fs/`) — the only sanctioned way to read/write/
  list/delete files or check existence, whether the path is local disk or CaskFS.
  A `cask:/`-prefixed path routes to CaskFS (`fs/cask.py`'s `CaskBackend`, over the
  CaskFS HTTP API); anything else is local disk (`fs/local.py`). Both backends raise
  the same builtin exceptions, so calling code never branches on which backend it hit.
  Key functions: `read`, `write`, `ls`, `exists`, `delete`, `add_derivative_link`
  (CaskFS lineage links). Don't shell out to the `cask` CLI or hit the CaskFS HTTP API
  directly from new code — the one existing exception is `digtk.models.collection`,
  which intentionally shells out to `cask cp` for its own documented reasons (rsync-style
  batch sync/progress/confirmation prompt); that's not a pattern to copy elsewhere.

- **`digtk.lib.derivative`** (`exec/digtk/lib/derivative.py`) — built on `fs`, for
  writing any silver/gold/workspace derivative artifact (thumbnails, OCR output, AI
  metadata, RAG chunks, etc.) and recording its CaskFS lineage back to source files.
  `write()` is the single entry point: it builds the destination path from
  `type`/`collection`/`rel_path`/`opts` and dispatches to `write_cask()` (real CaskFS
  write + lineage, requires non-empty `source_files`) or, with `path`+`local=True`,
  to `write_local()` (unchecked local-disk write, for debugging/testing only — no
  lineage). Use this instead of hand-building CaskFS derivative paths or calling
  `fs.write`/`fs.add_derivative_link` directly for pipeline outputs.

- **`digtk.lib.ai`** (`exec/digtk/lib/ai.py`) — the Samwise (Qwen, OpenAI-compatible)
  client. `chat_completion()` (text + optional vision input) for metadata generation,
  `embed()` for embeddings, `chunk_text()`/`chunk_and_embed()` for RAG chunking. Use
  this instead of calling the `openai` SDK or Samwise's API directly.

## Testing

Any change to `exec/digtk/lib/**` (adding, modifying, or removing behavior) requires
corresponding tests in `exec/tests/`. This includes new modules, new functions, and
behavior changes to existing functions — not just bug fixes.

Test file naming mirrors the module: `digtk/lib/ai.py` -> `tests/test_ai.py`,
`digtk/lib/fs/local.py` -> `tests/test_fs_local.py`. See `exec/tests/` for existing
examples and README.md for how to run the suite.
