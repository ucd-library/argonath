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
| A workspace folder, e.g. `cask:/workspace/jdoe/box-12` | Every item at or under it. Outputs go to that folder's `derivatives/`. |
| An ARK, e.g. `ark:/424242/d3gq6r65h` | That bronze item. Outputs go to silver. |

An **item** is the set of files sitting directly in one folder. A flat folder of scans
is one item. In a nested box/folder layout, each folder that holds files is its own item.

`--folder` and `--user` change where a local folder gets staged: the workspace folder name
and user. By default they are the local folder's own name and your OS user. A workspace
folder doesn't have to be a collection: it can be part of one, or span several.

### Scans without an ARK yet

To run several steps in one go:

```bash
digtk workspace run ./scans/box-12 --steps ocr,alt-text
```

```bash
digtk workspace run ./scans/box-12 --steps tag-pdf --tables --min-conf 60
```

```bash
digtk workspace run ./scans/box-12 --steps ocr --folder box-12-retry --user jdoe
```

- Steps always run in this order: `split-detect`, `split-apply`, `ocr`, `ocr-correct`,
  `alt-text`, `figures`, `pdf-build`, `tag-pdf`.
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

Options added from the standalone digtk repo:

- **`--long`** (`alt-text`): for an information-dense image (a map, poster, chart,
  diagram, table or mostly-text page), the same vision call also writes a
  `long_description` into `alt-text.json`. It's `null` for an ordinary photo.
- **`--ocr-enhance auto|gray`** (`ocr`, `tag-pdf`): contrast-boosts the image
  Tesseract reads, for faded originals such as mimeographs or pale typescript.
  `auto` keeps color and `gray` also converts to grayscale.
- **`--paragraphs`** (`tag-pdf`, needs `--tables`): tags each layout text block as
  one paragraph instead of one paragraph per line.

### Figures inside a page

`digtk accessibility figures` finds the charts, photos, diagrams, maps and drawings on
every page and writes alt text for each one.

- **Cost:** one Samwise call per page to find the figures, then one per informative
  figure to describe it.
- **Long descriptions:** charts, diagrams and maps also get a long description, since
  WCAG wants their information available, not just their existence.
- **Outputs:** per page, `figures.json` and a crop of each figure, under
  `accessibility/<file>/<page>/` (or `accessibility/<file>/` for an image). Each source
  file also gets `accessibility/<file>/figure-review.csv`, one row per figure.

To review, pull the CSV, then change what's needed:

- edit `alt` or `long_description`
- set `decorative` to `y` to leave a figure out of the reading order
- put `reject` in `flag` to drop a wrong detection

Upload it back to the same path. Reviewed rows win over the cached detection from
then on.

`tag-pdf --figures` and `build-pdf --figures` tag each informative figure as a
`Figure` element:

- **Alt text:** its alt text plus long description.
- **Caption:** a matching caption line next to it becomes its `Caption`.
- **Text inside:** axis labels and other text inside the box stay searchable and are
  read as part of the figure.
- **Reading order:** the figure is read where it sits on the page.
- **Pages not yet run** through `figures` are detected at tagging time. They won't get
  `--collection-context` then, so run `figures` first for collections that need it.
- **Box placement on rebuilt pages:** boxes are found on the unrotated page render. A
  rebuilt page that was deskewed can be off by the skew angle; boxes are padded for
  that.

```bash
digtk workspace run ./scans/report-7 --steps ocr,figures,tag-pdf --figures --tables
```

In Dagster, the `figures` checkbox runs the step and tags the figures. The per-item job
is `bronze_figures_job`.

### Handwriting: `--geometry qwen`

Tesseract finds little usable text in handwriting or very faint typescript. With
`--geometry qwen`, `tag-pdf` and `build-pdf` ask Samwise for each page's text lines
with their boxes, and build the invisible text layer from those instead.

- **Cost:** one call per page, re-asked at other image sizes when the answer comes
  back with fewer than 3 lines. The result is cached as `qwen-lines.json` next to the
  page's OCR output.
- **No deskew:** these pages are never deskewed.
- **No correction:** `--correct` doesn't apply, since the text is already the
  model's reading.
- **The `ocr` step** still uses Tesseract.

```bash
digtk accessibility build-pdf ./scans/letters-1 --geometry qwen
```

### Born-digital PDFs: tagging in place

`tag-pdf --mode` picks how a PDF is tagged:

- **`auto` (default):** in place when any page has real, extractable text; rebuilt
  when every page is a scan. Scanned PDFs behave as before.
- **`inplace`:** every page is kept exactly as produced: fonts, vector art, non-Latin
  text, links. The structure is added on top. Pages with no text (scans wrapped in
  the PDF) get an invisible OCR text layer.
- **`rebuild`:** every page is rendered to an image and rebuilt with an OCR text
  layer, as `tag-pdf` always did before.

Fonts and tools for in-place tagging:

- **Unembedded fonts:** PDF/UA requires every font to be embedded, and base-14
  Helvetica/Times/Courier from forms and older documents usually aren't. In place,
  `tag-pdf` rewrites such a PDF through Ghostscript first (`--embed-fonts`, on by
  default) and lists the fonts in `fonts_embedded`.
- **No Ghostscript:** the PDF is tagged as-is, the fonts are listed in
  `fonts_not_embedded`, and the result will fail veraPDF. Ghostscript is `gs` on the
  PATH, or set `DIGTK_GS_BIN`.
- **Links and form fields** are added to the structure tree, and an existing outline
  (bookmarks) is kept.
- **Review counts:** `untagged_images` counts images left as artifacts, and
  `unmatched_spans` counts text runs that didn't line up with the page. A few is
  normal; many means some words may be in the wrong element.
- **OCR on scan pages:** a scan page inside an in-place PDF is OCR'd without
  deskewing, so its word boxes land on the original page. It reuses the `ocr` step's
  output only when that output wasn't deskewed either.

`digtk ocr tesseract` now skips PDF pages that already have extractable text, since
their text is exact. It also only rasterizes the pages it OCRs. Pass
`--ocr-text-pages` to OCR them anyway.

```bash
digtk accessibility tag-pdf ark:/424242/d3gq6r65h --tables
```

```bash
digtk accessibility tag-pdf ark:/424242/d3gq6r65h --mode rebuild
```

### Building one PDF from a folder of page scans

`digtk accessibility build-pdf` builds an item's images into one searchable, tagged
PDF. It's the `docs fromimages` workflow from the standalone digtk repo.

- **Page order:** the images in natural filename order, so `page_2` comes before
  `page_10`.
- **Tagging:** each page is tagged the same way as `tag-pdf`, and the command takes
  the same options (`--tables`, `--correct` and the rest).
- **Alt text:** a page's alt text from `alt-text` is used as its figure description.
- **Outputs:** `tagged.pdf`, `searchable.pdf` and `text.txt` under
  `accessibility/document/`. `workspace promote` copies them like any other output.

As a workspace step it's `pdf-build`. It runs once per folder (so `--file` runs skip
it), after `ocr` so it can reuse that OCR:

```bash
digtk workspace run ./scans/letters-1 --steps ocr,pdf-build,alt-text --tables --correct
```

```bash
digtk accessibility build-pdf ark:/424242/d3gq6r65h --tables
```

`--skip-existing` (on `workspace run`, `tag-pdf` and `build-pdf`) resumes an
interrupted run. `ocr` and `alt-text` skip pages whose outputs exist, and `tag-pdf` /
`pdf-build` skip documents whose three outputs all exist. It doesn't check whether
those outputs are up to date with the current options.

### Reusing OCR output and correcting it

`tag-pdf` reuses what `digtk ocr tesseract` already published for a page when it was
made with the same image settings. It then skips the orientation check and Tesseract
pass. `ocr tesseract` now records those settings in `ocr-result.json`, so OCR output
from before this change isn't reused: run `ocr` again first to get the benefit. Pass
`--no-reuse-ocr` to always run Tesseract.

`digtk ocr correct` fixes Tesseract's spelling using the vision transcript, keeping
Tesseract's word boxes. It reads the published hOCR and `transcript.txt` and makes no
OCR or vision calls. It's deliberately conservative:

- **Never changed:** digits (only splitting a number like `1976.A` is allowed),
  one- or two-letter tokens, punctuation-only and case-only differences, and anything
  that would drop letters.
- **Skipped:** a page whose transcript doesn't line up with the OCR words.

It writes `ocr-corrected.json` next to each page's `ocr.hocr`, holding the corrected
words and a change log of every substitution.

```bash
digtk ocr correct ark:/424242/d3gq6r65h
```

`tag-pdf --correct` uses those corrected words for the text layer. If a page has no
correction yet, it makes one, as long as the page has reusable OCR output with a
transcript. Pages without one stay uncorrected and are listed in `uncorrected_pages`.
A correction made from older hOCR is ignored and redone.

```bash
digtk workspace run ./scans/box-12 --steps ocr,tag-pdf --correct
```

`tag-pdf` now publishes three files under `accessibility/<pdf>/`:

- `tagged.pdf`
- `searchable.pdf`: the same pages and text layer, without the structure tags
- `text.txt`: the whole document's text, one page per form feed. It uses the vision
  transcript where there is one, otherwise the OCR words.

What `tag-pdf --tables` produces from the page layout:

- **Headings** get the level the layout model reads (H1-H6). Levels never skip, so
  a level-3 heading right after an H1 becomes H2. Every heading is also a bookmark.
- **Lists** become L/LI with a separate label (`1.`, `a)`, bullet) and body.
- **Language of parts:** a block in another language than `--lang` gets its own
  language tag.
- **Reading order** follows the page: margin notes are read before the body line
  they sit beside, and multi-column pages are read column by column.
- **Cached layouts** (`layout.json`) carry a version. Ones made by the older prompt
  are regenerated on the next `--tables` run, which costs one Samwise call per page.

The searchable layer inside `tagged.pdf`:

- **The page image** is the unbinarized render, with the same resize and deskew as
  the OCR image, not the black-and-white image Tesseract reads.
- **The invisible text** uses an embedded, subset font (Droid Sans Fallback). Curly
  quotes, dashes, accents and CJK come through as written. Before this change they
  came out as `·`, so regenerate tagged PDFs made earlier.

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

The cut pages in `split-spreads/pages/` are a display derivative only. OCR, alt text,
figures and `pdf-build` always work from the original scans, so a built PDF has one
page per scan, with each spread as a single page. This is deliberate: a cut page is
derived from the original scan, not a replacement for it.

1. Detect gutters:

   ```bash
   digtk split-spreads detect ./scans/album-4
   ```

2. Download the review output:

   ```bash
   digtk workspace pull cask:/workspace/jdoe/album-4/derivatives ./review/album-4
   ```

3. Check `split-spreads/review-sheets/`. If a detected gutter is wrong, correct its
   entry in `split-spreads/gutters.json`, then upload the corrected file. The
   destination is the `split-spreads` folder, not the file:

   ```bash
   cask cp ./review/album-4/split-spreads/gutters.json /workspace/jdoe/album-4/derivatives/split-spreads -x
   ```

   `detect` never overwrites an existing entry, so your edits are safe if you run it
   again. A portrait image (taller than it is wide) is recorded as not a spread,
   with `"reason": "portrait"`, without a vision call.
   If you correct an entry from a spread to not a spread, also delete its old
   `pages/<stem>_L` and `_R` files with `cask rm`; `apply` doesn't remove them.
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

Job names start with a prefix so related jobs sort together:
- `workspace_` for workspace folders
- `bronze_` for the per-item tools on bronze ARK items
- `showcase_` for the showcase/`pdc_` pipeline

They were renamed from:

| Old | New |
|---|---|
| `process_workspace_job`, `promote_workspace_job` | `workspace_process_job`, `workspace_promote_job` |
| `ocr_job`, `correct_ocr_job`, `alt_text_job`, `figures_job` | `bronze_ocr_job`, `bronze_correct_ocr_job`, `bronze_alt_text_job`, `bronze_figures_job` |
| `tag_pdf_job`, `build_pdf_job` | `bronze_tag_pdf_job`, `bronze_build_pdf_job` |
| `detect_spreads_job`, `apply_spreads_job` | `bronze_detect_spreads_job`, `bronze_apply_spreads_job` |
| `process_collection_job`, `process_item_job`, `collection_rollup_job` | `showcase_process_collection_job`, `showcase_process_item_job`, `showcase_collection_rollup_job` |
| `reindex_collection_job`, `apply_cask_changes_job`, `derivatives_job` | `showcase_reindex_collection_job`, `showcase_apply_cask_changes_job`, `showcase_derivatives_job` |
| `delete_collection_index_job`, `delete_item_index_job` | `showcase_delete_collection_index_job`, `showcase_delete_item_index_job` |

Runs from before the rename stay under the old names in **Runs**. Filter by the old
name to find them, but their re-execute buttons no longer work. The `collection_state`
queue still uses the original keys (`process_item_job`, `collection_rollup_job`).

### Workspace folders (no ARK)

1. **Stage the folder** from a machine that can see it. The Dagster containers only
   reach CaskFS over HTTP and can't read host folders.

   ```bash
   digtk workspace add ./scans/box-12
   ```

   Or upload through the CaskFS web UI into `/workspace/<user>/<folder>/source/`,
   with one subfolder per item. The `source/` level is required. Without it, the
   sensor doesn't treat it as a workspace folder.

2. **Get a partition.** `workspace_partition_sync_sensor` adds a
   `workspace/<user>/<folder>` entry (e.g. `workspace/jdoe/box-12`) to the
   `workspace` partitions every 2 minutes. It also removes entries whose workspace
   folder is gone. Turn the sensor on under **Automation → Sensors**, because sensors
   start off. Alternatively, add the partition by hand from the job's partition picker.
3. **Run it.** Open `workspace_process_job` → **Launchpad**.
   1. Pick the partition (e.g. `workspace/jdoe/box-12`) in the **Partition** box at the
      top **first**, because choosing a partition reloads the config editor.
   2. Set the config, then click **Launch Run**. If it says **Launch backfill**, more
      than one partition is selected, and backfills ignore the config you typed.

   Every step and every on/off option defaults to `false`, so tick the steps you want.
   That includes `vision` (the transcript), `reuse_ocr`, `embed_fonts` and `validate`,
   which you'll usually want on. A backfill never sees launchpad edits, so it runs
   nothing until the defaults change. A typical config:

   ```yaml
   ops:
     fan_out_workspace:
       config:
         split_detect: true
         split_apply: true
         ocr: true
         ocr_correct: true
         alt_text: true
         figures: true
         pdf_build: true
         tag_pdf: true
         vision: true
         reuse_ocr: true
         embed_fonts: true
         validate: true
   ```

   - **One step per file.** OCR, alt text, and PDF tagging run as one Dagster step per
     file, named by the file's path in the workspace folder, e.g.
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
   - **PDF builds wait for the file steps.** `pdf_build` runs one step per folder that
     has images (`process_workspace_document[...]`), after every file step has
     finished, so it can reuse their OCR. If a file step fails outright, the builds are
     skipped; re-run with only `pdf_build` (and `skip_existing`) ticked.
   - **Resuming (`skip_existing`).** Skips pages that already have OCR or alt text,
     and documents whose `tagged.pdf`, `searchable.pdf` and `text.txt` all exist.
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
     `vision`, `ocr_enhance`, `collection_context`, `want_description`, `want_long`,
     `max_alt_chars`, `doc_lang`, `tables`, `strict`, `paragraphs`, `lookahead`,
     `min_conf`, `validate`, `mode`, `embed_fonts`, `ocr_text_pages`, `geometry`.
4. **Splitting spreads.** The default run detects the gutters and cuts the pages
   straight away, before anyone has reviewed `gutters.json`. If the review corrects
   any gutters, re-cut with a second run.
   1. Pull the review files with `digtk workspace pull`.
   2. Fix `split-spreads/gutters.json` if needed, and upload it back with
      `cask cp <local gutters.json> /workspace/<user>/<folder>/derivatives/<item path>/split-spreads -x`
      (leave out `<item path>/` for a flat folder). The destination is the folder;
      naming the file makes `cask cp` write `gutters.json/gutters.json` and fail.
   3. Launch again with only `split_apply: true`, with every other step set to `false`
      so OCR and alt text aren't regenerated.

   To run one file from the CLI the same way a Dagster file step does, use:
   `digtk workspace run cask:/workspace/<user>/<folder>/source/<item folder> --steps ocr --file page_004.tif`.

### Launching from your laptop

`argonath workspace run` (the ops CLI in `ops/`) launches `workspace_process_job` for one
workspace folder on the VM's Dagster, without SSH.

1. **Log in.** This one login covers CaskFS and Dagster:

   ```bash
   uv run digtk auth login --cask-env argonath-vm
   ```

2. **Stage the folder:**

   ```bash
   uv run digtk workspace add ../../workspace/<sourceFolder> --folder <folderName> --user <casId>
   ```

3. **Launch.** It waits for `workspace_partition_sync_sensor` to register the
   `workspace/<casId>/<folderName>` partition if it's new (up to about 3 minutes; the
   sensor must be on), starts the run, and prints its Dagster link. `--wait` polls until
   it finishes. Dagster's API can't add a partition used only by op jobs, which is why the
   CLI relies on the sensor.

   ```bash
   argonath workspace run cask:/workspace/<casId>/<folderName> --steps all,vision,reuse-ocr,embed-fonts,validate --wait
   ```

4. **Pull the outputs** when it's done:

   ```bash
   uv run digtk workspace pull cask:/workspace/<casId>/<folderName>/derivatives ../../workspace/<exportFolder>
   ```

Every step defaults to off, so `--steps` is required:

- **`--steps`** runs the listed steps, e.g. `--steps ocr,figures,tag-pdf`, or `all` for
  every step. It also takes the names of on/off options, which turns them on:
  - `vision`, `reuse-ocr`, `embed-fonts`, `validate` (you'll usually want all four)
  - `want-description`, `want-long`, `tables`, `strict`, `paragraphs`, `correct`,
    `skip-existing`, `ocr-text-pages`

  Without `vision`, OCR makes no transcript, so `ocr-correct` and `correct` skip every
  page. The common full run, the same as `all` plus the usual options:
  `--steps split-detect,split-apply,ocr,ocr-correct,alt-text,figures,pdf-build,tag-pdf,vision,reuse-ocr,embed-fonts,validate`.
- **`--set key=value`** sets any `fan_out_workspace` option by its config name, for the
  text and number options `--steps` can't express, e.g. `--set min_conf=60
  --set collection_context="Wine collection papers"`. Repeat it for more options.
  Names aren't checked until Dagster validates the launch.
- **`--config run.json`** reads a JSON object of `fan_out_workspace` options.
  `--steps` and `--set` win over it.
- **`--dry-run`** prints the Dagster URL, the partition (and whether it exists yet) and the
  run config without launching.

How `argonath` reaches Dagster:

- **Which Dagster:** `--dagster-url`, else `DAGSTER_URL`, else your `digtk auth login`
  gateway (e.g. `https://argonath.library.ucdavis.edu/dagster`), else
  `http://localhost:3000/dagster`.
- **Token:** the login token is sent only to that login's own host.
- **Logged out:** a gateway redirect becomes an error telling you to log in.
- **Role:** launching runs needs the `execute` (or `admin`) role on the `argonath`
  Keycloak client.
- **No SAMWISE_API_KEY needed** on the laptop; the Dagster workers have their own.
- **Broker settings:** celery's broker settings are built into the job's executor
  (`celery_job_executor()`), so launches from the CLI, sensors and the launchpad all
  reach the deployment's RabbitMQ. The launchpad no longer shows an `execution:` block.

### Bronze items (with ARKs)

- **Collection partitions** are keyed `bronze/<group>/<collection>` (e.g.
  `bronze/preservica/haeger`). `collection_partition_sync_sensor` registers them for
  anything added with `digtk collection add --root bronze` or `digtk preservica import`.
  Partitions under the older `<group>/<collection>` form aren't removed automatically;
  delete them in the UI once they're no longer needed.
- **Item partitions** (ARKs) are registered by `reconcile_collection_job`.
- **Per-item tool jobs:** `bronze_ocr_job`, `bronze_correct_ocr_job`, `bronze_alt_text_job`, `bronze_figures_job`,
  `bronze_tag_pdf_job`, `bronze_build_pdf_job`, `bronze_detect_spreads_job`, and `bronze_apply_spreads_job`. Pick a (collection, item) partition and
  set the asset config. They only depend on the item being ingested, and each one
  records reporting under the Dagster run ID. `bronze_correct_ocr_job` needs `bronze_ocr_job`'s output.
- **Webapp derivatives for a whole collection:** `showcase_process_collection_job` (derivatives
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

- **What the index reads:** an item's ES doc is built from its gold `item.jsonld.json`
  (descriptive metadata, media nodes, and the bronze folder named in the gold
  `manifest.json`) plus silver (renditions, captions, OCR). Indexing never reads bronze.
  Only `--rebuild` / `rebuild` does, because it runs `build-item` to rebuild gold first.
- **Showcase search index for a whole collection:** `showcase_reindex_collection_job` sends every
  item with a gold record to Elasticsearch with the `_bulk` API. It doesn't make
  derivatives. With `rebuild`, it lists the collection's bronze items instead and
  rebuilds each gold record first. The prune still compares ES with bronze.
  - **Steps:** one `reindex_batch[...]` step per `batch_size` items (default 200), each
    running `digtk showcase es upsert-items <folders...>`. With `rebuild` set, it first
    runs `build-item` on each item, which picks up bronze metadata edits. Items without
    an ARK are skipped.
  - **Prune:** `prune_collection_index` deletes indexed items whose ARK is no longer in
    the collection's bronze items, along with their page docs. It's skipped if any
    batch failed. It also refuses to run when bronze lists no items, or when it would
    delete more than half of the indexed items. Set `force_prune` to override.
  - **Collection doc:** `pdc_index_collection` then refreshes `itemCount` and the image,
    the same step `showcase_collection_rollup_job` ends with.
- **Automatic updates from the CaskFS audit log:** `bronze_cask_audit_sensor` (every
  5 minutes; turn it on in the UI) is the only sensor that reacts to bronze `tree/`
  changes. It reads `caskfs.audit_log` straight from Postgres for writes, deletes and
  moves under each active collection's bronze items root. Once an item has had no new
  events for 5 minutes, the sensor handles it:
  - **New item, or binaries changed (`process`):** the item is queued in
    `collection_state.job_queue` (trigger `cask-audit`). `item_queue_sensor` launches one
    `showcase_process_item_job` run per item, at most `ARGONATH_MAX_ITEM_RUNS_IN_FLIGHT` (default
    20) at a time. Each run fans out its own files: derivatives, finalize, `build-item`,
    then the ES upsert. Once a collection's queue is empty and none of its item runs are
    in flight, `collection_rollup_sensor` launches `showcase_collection_rollup_job`, which records
    the rollup and rebuilds the collection doc. It fails if any item's latest run failed.
  - Everything else launches one `showcase_apply_cask_changes_job` run per changed collection:
  - **Only `metadata.json`, `identifiers.json` or `*.jsonld.json` changed (`reindex`):**
    `build-item` and the ES upsert, with no derivatives.
  - **Folder deleted (`delete` + `cleanup`):** its ES docs are deleted (using the ARK
    from its gold record), then its silver directory and its directory in every gold
    project (`digtk showcase clean-item`). Each removed directory is logged with its
    file count and recorded as an `argonath_fanout_cleanup` event.
  - **Folder renamed:** processed at its new path. Its ES doc is kept, since the ARK is
    the same, but the old path's silver and gold are removed. The doc's images point at
    the old silver files until the new path's `showcase_process_item_job` run finishes.
  - **Collection `tree/metadata.json` or `tree/identifiers.json` changed:** the run has
    empty lists and only rebuilds the collection doc.
  - Every `showcase_apply_cask_changes_job` run ends by rebuilding and reindexing the collection doc.
  - To preview a cleanup by hand:
    `digtk showcase clean-item <bronze item folder> --dry-run`.
  - Preservica syncs feed it too, through the `tree/` writes and deletes they make.
  - Settings: `DIGTK_CASKFS_PG_*` (host, port, database, user, password, schema)
    point at CaskFS's database; each falls back to the `DIGTK_REPORTING_PG_*` value.
    CaskFS must run with `CASKFS_AUDIT_ENABLED=true`.
  - Only registered collections are watched, so onboard a new one with
    `showcase_process_collection_job` first.
  - The other jobs (`showcase_reindex_collection_job`, the delete jobs) are manual only.
    `showcase_process_item_job` and `showcase_collection_rollup_job` can also be launched by hand.
- **Items removed by a Preservica sync:** when a synced folder (SO) no longer lists a
  child that Preservica knew about (it has a `by-ref/` entry), the tree rebuild deletes
  that child's `tree/` entry. `by-ref/` is kept. `bronze_cask_audit_sensor` then sees the
  delete and removes its ES docs.
  - To remove an item, remove it in Preservica and sync its parent folder. In the
    launchpad, `preservica_changed` names the parent with `type: SO`, not the item.
  - Items added directly in bronze `tree/` (no `by-ref/` entry yet) are never deleted by a
    sync, whether or not their `metadata.json` has a `preservica.ref`.
  - The full bronze-vs-ES diff (`es prune`) only runs by hand or in
    `showcase_reindex_collection_job`.
- **Removing from the search index:** `showcase_delete_collection_index_job` (collection partition)
  deletes the collection doc, its items and their pages. `showcase_delete_item_index_job` (config
  `ark`) deletes one item and refreshes its collection doc. Both touch ES only, so an
  item that's still in bronze comes back on the next reindex.
- **The same from a terminal:**

  | Task | digtk (runs directly) | argonath (launches the Dagster job) |
  |---|---|---|
  | Reindex a collection | `digtk showcase es bulk-upsert <key> [--rebuild] [--no-prune]` | `argonath index collection <key> [--rebuild] [--no-prune] [--wait]` |
  | Prune only | `digtk showcase es prune <key> [--dry-run] [--force]` | n/a |
  | Delete a collection | `digtk showcase es delete-collection <key>` | `argonath index delete-collection <key> [--wait]` |
  | Delete an item | `digtk showcase es delete <ark>` | `argonath index delete-item <ark> [--collection <key>] [--wait]` |

  `<key>` is `bronze/<group>/<collection>`. For which Dagster `argonath` talks to, see
  [Launching from your laptop](#launching-from-your-laptop).

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

Open `workspace_promote_job` → **Launchpad**, pick the workspace partition (e.g.
`workspace/jdoe/box-12`), and fill in the ARK. The config editor opens pre-filled with every
field, `ark` empty and `dry_run: true`. Use the full ARK, e.g. the `@id` from the
item's bronze sidecar. Promote works out the silver location from where that ARK
lives in bronze. For the usual case, where the workspace folder is a single
item, that's all you need:

```yaml
ops:
  promote_workspace_items:
    config:
      ark: ark:/424242/d3gq6r65h
      dry_run: true
```

For a workspace folder that holds several items, list each item folder under `source/` with
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

## Tesseract timeouts

Every Tesseract call is limited to `DIGTK_IMAGE_TIMEOUT_S` (default 120 s) and retried
once after a timeout. Each call runs with `OMP_THREAD_LIMIT=1` (`DIGTK_OCR_THREADS`,
default 1) so parallel celery steps don't fight over CPU. An `OMP_THREAD_LIMIT` already
in the environment wins. Without the limit, each Tesseract process starts a thread per
core, and parallel PDF steps time out during "deciding orientation".

## Samwise timeouts

Vision calls (transcripts, alt text, split-detect, table layout) stream Samwise's
response, so a long answer that is still arriving never times out. These settings
control the behavior. Set each one with an env var, or persist it with
`uv run digtk config set <setting> <value>`.

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `samwise.llm_model` | `DIGTK_SAMWISE_LLM_MODEL` | `qwen3.8-fp8-fast:27b` | Vision/chat model. A "Model not found" (400) error means Samwise no longer serves this name; requests Samwise rejects (4xx) fail at once instead of retrying. |
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

- By design, OCR, alt text, figures and `pdf-build` use the original scans, not cut
  split-spread pages. `split-apply` output is a display derivative and isn't fed back
  into later steps (see "Splitting two-page spreads").
- `tag-pdf` reuses the `ocr` step's word boxes only when both ran with the same image
  settings (`--ocr-enhance`, size, DPI, deskew threshold). Otherwise it runs its own
  orientation check and Tesseract pass.
- Silver `ocr/*.hocr` files written by older versions of `digtk ocr tesseract` are
  left in place. Current output goes to `text/`.
