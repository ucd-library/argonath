# Running OCR, Accessibility, and Split-Spreads Tools

How to run the root-level digtk tools (`ocr`, `accessibility`, `split-spreads`) from
the CLI and from Dagster. It covers scans that don't have an ARK yet (staged in
CaskFS workspace) and items that do (bronze/silver), and how to promote workspace
results into silver once an item has been ingested.

The tools prepare their own inputs from the source files (orientation detection,
OCR-prep images, PDF rasterization). None of them need `digtk showcase ... derivatives`
to have run first. For where outputs land, see
[cask-conventions.md](cask-conventions.md) ("Root-Level Tool Outputs" and "Workspace
Layout").

## Setup

### Local dev environment

From `argonath-deployment/`, build the images (first time, or after a full rebuild),
bring up the stack, then initialize it. `init.sh` no longer builds or starts anything
itself, so the stack must already be running:

```bash
./cmds/build-local-dev.sh --all
```

```bash
./cmds/up.sh
```

```bash
./cmds/init.sh
```

`init.sh` sets up the CaskFS tables and admin roles and creates the `argo-local-dev`
cask CLI environment (`http://localhost:4000/cask`). It then logs digtk in through the
local auth gateway.

### Connecting digtk to CaskFS

digtk needs a CaskFS connection. There are two ways to set one up.

**Option 1: log in through the gateway (recommended).** `init.sh` already does this.
To redo it by hand, run this from `argonath/exec`:

```bash
DIGTK_GATEWAY_URL=http://localhost:4000 uv run digtk auth login --cask-env argo-local-dev
```

- This stores `api_url = http://localhost:4000/cask/api` plus a token in
  `~/.digtk/credentials.json`, so you don't need to set `CASK_API_URL` or `CASK_API_KEY`.
- `--cask-env` also writes the token into the `cask` CLI environment. Staging local
  folders (`digtk workspace add`) uses that environment.
- Without `DIGTK_GATEWAY_URL`, login goes to the production gateway
  (`https://argonath.library.ucdavis.edu`). If you logged in before `init.sh` set it,
  run the command above again to replace the stored prod credentials.
- `CASK_API_KEY` holds the same token. Only set it to override the stored login.

**Option 2: talk to CaskFS directly on :3001 (local only).** This is what the Dagster
containers do. They skip the gateway and send a trusted-user header:

```bash
export CASK_API_URL=http://localhost:3001/cask/api
```

```bash
export CASK_TRUSTED_USER=<your-username>
```

This works because `init.sh` makes your username a cask admin. It depends on CaskFS
trusting that header, so it never works against a deployed environment. Staging local
folders still goes through the `argo-local-dev` cask environment.

### Other requirements

- **`SAMWISE_API_KEY`** is needed for vision OCR, alt text, split-detect, and
  `tag-pdf --tables`. It's set in `compose/local-dev/.env` for the containers; export it
  in your shell for the CLI. Use `digtk ocr tesseract --no-vision` to OCR without it.
- **The `cask` CLI** must be installed and configured wherever you stage a local folder.
  Staging shells out to `cask cp`.
- **Imaging tools** are needed if you run digtk on your own machine: `tesseract`,
  ImageMagick, poppler (`pdfinfo`), Ghostscript, and `verapdf` for tag-pdf (or pass
  `--no-validate`). The worker image already has all of them.

## CLI

Every tool command takes a **target**, which can be any of:

| Target | What happens |
|---|---|
| A local folder, e.g. `./scans/box-12` | Uploaded to `cask:/workspace/$USER/box-12/source/` first. Outputs go to `.../box-12/derivatives/`. |
| A workspace path, e.g. `cask:/workspace/jdoe/box-12` | Every item at or under it. Outputs go to that collection's `derivatives/`. |
| An ARK, e.g. `ark:/424242/d3gq6r65h` | That bronze item. Outputs go to silver. |

An **item** is the set of files sitting directly in one folder. A flat folder of scans
is one item. In a nested box/folder layout, each folder that holds files is its own item.

`--collection` and `--user` change where a local folder gets staged. By default they
are the folder's own name and your OS user.

### Scans without an ARK yet

To run several steps in one go:

```bash
digtk workspace run ./scans/box-12 --steps ocr,alt-text
```

```bash
digtk workspace run ./scans/box-12 --steps tag-pdf --tables --min-conf 60
```

```bash
digtk workspace run ./scans/box-12 --steps ocr --collection box-12-retry --user jdoe
```

- Steps always run in this order: `split-detect`, `split-apply`, `ocr`, `alt-text`,
  `tag-pdf`.
- A step that doesn't apply to an item is skipped for that item. For example,
  `tag-pdf` is skipped for a folder of images.

Individual tools accept the same targets:

```bash
digtk ocr tesseract ./scans/box-12
```

```bash
digtk accessibility alt-text cask:/workspace/jdoe/box-12 --want-description
```

```bash
digtk accessibility tag-pdf cask:/workspace/jdoe/box-12/source/folder-3 --tables --min-conf 60
```

To upload now and process later:

```bash
digtk workspace add ./scans/box-12
```

```bash
digtk workspace ls
```

```bash
digtk workspace items cask:/workspace/jdoe/box-12
```

### Splitting two-page spreads

Splitting is a two-step process with a human review in between.

1. Detect gutters:

   ```bash
   digtk split-spreads detect ./scans/album-4
   ```

2. Download the review output:

   ```bash
   digtk workspace pull cask:/workspace/jdoe/album-4/derivatives ./review/album-4
   ```

3. Check `split-spreads/review-sheets/`. If a detected gutter is wrong, correct its
   entry in `split-spreads/gutters.json`, then upload the corrected file to the same
   CaskFS path. `detect` never overwrites an existing entry, so your edits are safe if
   you run it again.
4. Cut the pages:

   ```bash
   digtk split-spreads apply cask:/workspace/jdoe/album-4
   ```

Don't tick both split steps in a single `workspace run`, because that skips the review.

### Items that already have an ARK

The same commands with an ARK write straight to silver. Lineage points at the bronze
binaries, and the item's catalog record is used as context for alt text.

```bash
digtk ocr tesseract ark:/424242/d3gq6r65h
```

```bash
digtk workspace run ark:/424242/d3gq6r65h --steps ocr,alt-text,tag-pdf
```

### Reporting

Add `--reporting-job-id <id>` (and optionally `--reporting-collection <key>`) to any
tool command, or to `workspace run`. This records one row per item in the reporting
tables. Without it, nothing is recorded.

## Dagster UI

The Dagster UI is at http://localhost:3000 in local dev.

### Workspace collections (no ARK)

1. **Stage the folder** from a machine that can see it. The Dagster containers only
   reach CaskFS over HTTP and can't read host folders.

   ```bash
   digtk workspace add ./scans/box-12
   ```

   Or upload through the CaskFS web UI into `/workspace/<user>/<collection>/source/`,
   with one subfolder per item. The `source/` level is required. Without it, the
   sensor doesn't treat the folder as a workspace collection.

2. **Get a partition.** `workspace_partition_sync_sensor` adds a
   `workspace/<user>/<collection>` entry (e.g. `workspace/jdoe/box-12`) to the
   `workspace` partitions every 2 minutes. It also removes entries whose workspace
   folder is gone. Turn the sensor on under **Automation → Sensors**, because sensors
   start off. Alternatively, add the partition by hand from the job's partition picker.
3. **Run it.** Open `process_workspace_job` → **Launchpad**.
   1. Pick the partition (e.g. `workspace/jdoe/box-12`) in the **Partition** box at the
      top **first**, because choosing a partition reloads the config editor.
   2. Set the config, then click **Launch Run**. If it says **Launch backfill**, more
      than one partition is selected, and backfills ignore the config you typed.

   By default it runs every step: split-detect, split-apply, OCR, alt text (with a
   catalog description, `want_description: true`), and PDF tagging. A backfill, which
   never sees launchpad edits, does the same. Untick steps or change options in the
   config:

   ```yaml
   ops:
     fan_out_workspace:
       config:
         split_detect: true
         ocr: true
         alt_text: true
         tag_pdf: true
         tables: false
   ```

   - **One step per file.** OCR, alt text, and PDF tagging run as one Dagster step per
     file, named by the file's path in the collection, e.g.
     `process_workspace_file[source_d3bv7b33r_page_004_tif]`. A folder with 10 TIFFs
     and a PDF gives 11 file steps, and each can be retried on its own. PDF tagging
     only runs for PDFs.
   - **Batching (`max_steps`).** No fan-out produces more than `max_steps` steps
     (launchpad config; `0` turns batching off). Past that, several files or folders
     share one step and run one after another inside it.
     - **Step name:** the files' paths joined with `__`, cut off before 200
       characters with `__and_N_more__<hash>`. Dagster stores each step's input in a
       file named after the step, so names must fit the filesystem's limit.
     - **Events:** the step's Events start with `batch of N: <paths>`, and every file
       still gets its own result event.
     - **Decision:** the fan-out logs it, e.g.
       `files: 361 task(s) > max_steps 30 -> batched into 28 step(s) of up to 13`.
   - **One step per folder.** Split-spreads detect/apply runs as one step per item
     folder (`process_workspace_folder[...]`), because `gutters.json` and the contact
     sheets cover the whole folder.
   - **Same outputs.** Per-file steps write exactly the paths a whole-folder run would.
   - **Concurrency.** Up to 5 steps run at once (celery `worker_concurrency`), so up to
     5 Samwise calls at a time.
   - **Failures.** A step that fails doesn't stop the file's other steps. It shows as
     `<step>: FAILED -> ...` in the event log and the run is `partial`, so check the
     event log rather than relying on the green check. A file step only fails if every
     one of its steps failed.
   - **Reporting.** Each file (`<folder>#<file>`) or folder, and step, gets a reporting
     row under the run ID.
   - **Checking a run.** Each step's first log line is the
     `digtk workspace run ... --steps ... [--file ...]` it ran, which shows what
     actually ran.
   - Every tool option is available in the config: `max_px`, `lang`, `psm`, `oem`,
     `vision`, `collection_context`, `want_description`, `max_alt_chars`, `doc_lang`,
     `tables`, `min_conf`, `validate`.
4. **Splitting spreads.** The default run detects the gutters and cuts the pages
   straight away, before anyone has reviewed `gutters.json`. If the review corrects
   any gutters, re-cut with a second run.
   1. Pull the review files with `digtk workspace pull`.
   2. Fix `split-spreads/gutters.json` if needed, and upload it back with
      `cask cp <local gutters.json> /workspace/<user>/<collection>/derivatives/split-spreads/gutters.json -x`.
   3. Launch again with only `split_apply: true`, with every other step set to `false`
      so OCR and alt text aren't regenerated.

   To run one file from the CLI the same way a Dagster file step does, use:
   `digtk workspace run cask:/workspace/<user>/<collection>/source/<folder> --steps ocr --file page_004.tif`.

### Bronze items (with ARKs)

- **Collection partitions** are keyed `bronze/<group>/<collection>` (e.g.
  `bronze/preservica/haeger`). `collection_partition_sync_sensor` registers them for
  anything added with `digtk collection add --root bronze` or `digtk preservica import`.
  Partitions under the older `<group>/<collection>` form aren't removed automatically;
  delete them in the UI once they're no longer needed.
- **Item partitions** (ARKs) are registered by `reconcile_collection_job`.
- **Per-item tool jobs:** `ocr_job`, `alt_text_job`, `tag_pdf_job`,
  `detect_spreads_job`, and `apply_spreads_job`. Pick a (collection, item) partition and
  set the asset config. They only depend on the item being ingested, and each one
  records reporting under the Dagster run ID.
- **Webapp derivatives for a whole collection:** `process_collection_job` (derivatives
  only).
  - **Steps:** one step per image and per PDF file (`process_derivative_batch[...]`),
    each running `digtk showcase image|pdf derivatives <ark> --file <bronze path>`.
    Video and audio stay one step per item.
  - **Finalize:** after every derivative step, a `finalize_batch[...]` step per item
    (`digtk showcase finalize <ark>`) writes what combines all of an item's files: an
    image item's `ocr/manifest.json` and the item's `associatedMedia`.
  - **Source files:** each item's files come only from its own bronze `tree/` folder
    (there's no fallback to `by-ref/`). An item whose folder has no media is reported
    as an error, `[latest/tree/<item>] MISSING MEDIA: no image/PDF/video/audio files in
    bronze latest/tree/<item>/`, and that item isn't finalized. Every other item still
    processes, and the run ends failed.
  - **Naming:** steps and Events use the file's (or item's) bronze path relative to the
    partition, e.g. `latest/tree/mv-rapleytrail-formatted/Preservation/page_003.tif`.
    Step names show it with `_` in place of `/` and `.`.
  - **Batching:** `max_steps` (on `fan_out_derivative_tasks`; `0` turns batching off)
    caps both the derivative and finalize fan-outs, the same way as in the workspace job
    above.
  - **Failures:** an item with a failed file step isn't finalized. Every other item
    still is, and `check_derivative_failures` marks the run failed. Re-run the
    partition, or the failed file by hand, then finalize that item:

    ```bash
    digtk showcase image derivatives <ark> --file <bronze path>
    ```

    ```bash
    digtk showcase finalize <ark>
    ```

## Promoting workspace results to silver

Once scans processed in workspace have been ingested to bronze under an ARK, you can
copy the finished outputs into silver instead of regenerating them.

1. **Ingest the originals to bronze** as usual, so the item resolves under an ARK. The
   bronze filenames must match the ones in the workspace `source/` folder.
2. **Preview the copy:**

   ```bash
   digtk workspace promote cask:/workspace/jdoe/box-12/source/folder-3 --ark ark:/424242/d3gq6r65h --dry-run
   ```

3. **Promote:**

   ```bash
   digtk workspace promote cask:/workspace/jdoe/box-12/source/folder-3 --ark ark:/424242/d3gq6r65h
   ```

What promote does:

- It copies `text/`, `accessibility/`, and `split-spreads/` into the ARK's silver
  location unchanged, with no OCR or vision calls re-run.
- It re-points lineage at the same-named bronze binaries.
- It refuses to run if a workspace file has no same-named bronze binary.
- It refuses to overwrite existing silver outputs unless you pass `--replace`.

### From the Dagster UI

Open `promote_workspace_job` → **Launchpad**, pick the workspace partition (e.g.
`workspace/jdoe/box-12`), and fill in the ARK. The config editor opens pre-filled with every
field, `ark` empty and `dry_run: true`. Use the full ARK, e.g. the `@id` from the
item's bronze sidecar. Promote works out the silver location from where that ARK
lives in bronze. For the usual case, where the collection is a single
item, that's all you need:

```yaml
ops:
  promote_workspace_items:
    config:
      ark: ark:/424242/d3gq6r65h
      dry_run: true
```

For a collection that holds several items, list each item folder under `source/` with
its own ARK:

```yaml
ops:
  promote_workspace_items:
    config:
      items:
        - source: folder-3
          ark: ark:/424242/d3gq6r65h
        - source: folder-4
          ark: ark:/424242/d7abc1234
      dry_run: true
```

For a batch whose item folders are named after their ARK ids (e.g.
`source/d3bv7b33r` is `ark:/87293/d3bv7b33r`), skip the list and derive every ARK
from the folder names:

```yaml
ops:
  promote_workspace_items:
    config:
      ark_from_folder: true
      naan: '87293'
      dry_run: true
```

- **The NAAN** (Name Assigning Authority Number) is the number after `ark:/`. It
  identifies the organization that issued the ARKs. It's required here, and there's
  no default, because a wrong NAAN produces ARKs that don't exist.
- **Which folders:** every item folder in the partition is promoted as
  `ark:/<naan>/<folder name>`. For nested folders, the last part of the path is used.
- **Overrides:** a folder whose name isn't its ARK can still be listed under `items`,
  and that entry takes precedence.
- **Files directly in `source/`** have no folder name, so they're skipped with a
  warning.
- **The end-of-run summary** reports how many items are ready (or promoted) and how
  many failed.

1. Launch with `dry_run: true` (the default). The event log lists every silver file
   each item would be copied to. A folder whose name isn't a real ARK shows as
   `promote: FAILED -> ...` and the others continue.
2. Launch again with `dry_run: false` to copy.

Each entry runs as its own `digtk workspace promote`. An entry that fails (no bronze
item for the ARK, mismatched filenames, or outputs already in silver) is logged as
`promote: FAILED -> ...` and the rest continue. The run only fails if every entry
failed. Set `replace: true` to overwrite outputs already in silver.

**Alternative:** re-run the steps on the ARK instead (see
[Items that already have an ARK](#items-that-already-have-an-ark)). This repeats the
OCR and vision calls, but alt text then gets the catalog metadata that the workspace
run didn't have.

## Samwise timeouts

Vision calls (transcripts, alt text, split-detect, table layout) stream Samwise's
response, so a long answer that is still arriving never times out. These settings
control the behavior. Set each one with an env var, or persist it with
`uv run digtk config set <setting> <value>`.

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `samwise.read_timeout_s` | `DIGTK_SAMWISE_READ_TIMEOUT_S` | 180 | Give up when Samwise has sent nothing for this long (a stalled backend), then retry. |
| `samwise.max_call_s` | `DIGTK_SAMWISE_MAX_CALL_S` | 900 | Overall cap on one call. Hitting it is not retried, and in OCR/alt-text that page is skipped and reported. |
| `samwise.retries` | `DIGTK_SAMWISE_RETRIES` | 6 | Attempts per call for connection errors or stalls. |
| `samwise.stream` | `DIGTK_SAMWISE_STREAM` | true | Set `false` to go back to single, non-streamed requests. |
| `samwise.progress_every_s` | `DIGTK_SAMWISE_PROGRESS_EVERY_S` | 30 | How often a call in progress logs how much it has received. |

Each streamed call logs a timeline to stderr. In Dagster, it shows in the step's
stdout logs.

```
digtk: Samwise first response after 3s
digtk: Samwise still responding (30s, 900 chars so far)
digtk: Samwise still responding (60s, 1850 chars so far)
digtk: Samwise done in 74s (2300 chars)
```

A "first response" that only arrives at the very end, right before "done", means
something between digtk and Samwise is buffering the stream. Streaming then behaves
like a single long request, and whoever runs the proxy needs to turn buffering off.

## Known limitations

- OCR runs on the original scans, not on cut split-spread pages. `split-apply` output
  isn't fed back into later steps.
- `ocr` and `tag-pdf` each run their own orientation check, which is two Tesseract
  passes per page.
- Silver `ocr/*.hocr` files written by older versions of `digtk ocr tesseract` are
  left in place. Current output goes to `text/`.
