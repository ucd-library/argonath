# argonath

## Testing

Any change to `exec/digtk/lib/**` (adding, modifying, or removing behavior) requires
corresponding tests in `exec/tests/`. This includes new modules, new functions, and
behavior changes to existing functions — not just bug fixes.

Test file naming mirrors the module: `digtk/lib/ai.py` -> `tests/test_ai.py`,
`digtk/lib/fs/local.py` -> `tests/test_fs_local.py`. See `exec/tests/` for existing
examples and README.md for how to run the suite.
