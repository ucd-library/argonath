"""
Workspace staging for the root-level tools (ocr, accessibility, split-spreads)
— running them on scans before they have an ARK or a bronze copy.

Layout, per docs/cask-conventions.md's workspace section:

  /workspace/[username]/[collection]/
    source/        staged source binaries (a local folder's contents, as-is)
    derivatives/   every tool output, mirroring source/'s item paths

An item is every file sitting directly in one source/ directory (see
digtk.lib.items.resolve_workspace()): a flat folder of scans is one item, a
nested box/folder layout is one item per folder that holds files.

This module owns the parts of that workflow that aren't plain resolution:
staging a local folder in (stage(), via digtk.models.collection.add()'s
`cask cp`), turning any accepted target into its items (resolve_targets()),
running a chosen set of tool steps over them (run()), pulling outputs back to
local disk for review (pull()), and promoting a reviewed workspace item's
outputs into silver once it has a bronze copy and an ARK (promote()).
"""
import os

from digtk.lib import derivative, fs, items, reporting, utils
from digtk.models import accessibility, ocr, split
from digtk.models import collection as collection_model
from digtk.models import prep

# One large page per listing — see digtk.lib.items._WORKSPACE_LS_OPTS.
_LS_OPTS = {"limit": 10000}

# Every step `run()` can execute, in the order they always run: split before
# OCR (so a human-reviewed split is available before anything else looks at the
# pages), alt text before PDF tagging (tag_pdf reads alt-text.json back if present).
# Each entry: (callable, formats the item needs at least one binary of).
STEPS = {
    "split-detect": (split.detect, {"image"}),
    "split-apply": (split.apply, {"image"}),
    "ocr": (ocr.run_ocr, {"image", "pdf"}),
    "alt-text": (accessibility.generate_alt_text, {"image", "pdf"}),
    "tag-pdf": (accessibility.tag_pdf, {"pdf"}),
}
STEP_ORDER = list(STEPS)

# Steps that work across a whole item folder rather than file by file —
# split-spreads keeps one gutters.json and one set of contact sheets per item,
# so it can't be split into one run per file (see run()'s 'files' opt).
FOLDER_STEPS = {"split-detect", "split-apply"}

# Top-level output directories each tool writes under an item's derivatives/ root
# (plus a PDF stem, for OCR's namespaced per-PDF text/ dirs — see models/ocr.py).
_TOOL_DIRS = {ocr.TEXT_DIR, "accessibility", "split-spreads"}


def stage(local_dir, opts={}):
    """Upload a local folder into a workspace collection's source/ directory.

    Re-running over the same folder only transfers what's changed (`cask cp`
    does its own hash-based sync — see digtk.models.collection.add()).

    :param str local_dir: Local directory to upload.
    :param dict opts: 'collection' (str, default the folder's own name),
        'username' (str, default the current OS user), 'yes' (bool, default True
        — skip cask cp's confirmation prompt), 'env' (cask CLI environment),
        'replace' (bool), 'run' (injectable subprocess.run, for tests).
    :returns: The workspace collection root, e.g. 'cask:/workspace/jdoe/box-12'.
    :rtype: str
    :raises ValueError: If `local_dir` isn't a directory.
    :raises subprocess.CalledProcessError: If cask cp fails.
    """
    if not os.path.isdir(local_dir):
        raise ValueError(f"not a local directory: {local_dir}")
    collection = opts.get("collection") or os.path.basename(os.path.abspath(local_dir))
    add_opts = {
        "username": opts.get("username"),
        "subdir": items.WORKSPACE_SOURCE_DIR,
        "yes": opts.get("yes", True),
        "env": opts.get("env"),
        "replace": opts.get("replace", False),
    }
    if opts.get("run"):
        add_opts["run"] = opts["run"]

    root = collection_model.dest_path("workspace", collection, {"username": add_opts["username"]})
    utils.log(f"Staging {local_dir} -> cask:{root}/{items.WORKSPACE_SOURCE_DIR}")
    collection_model.add(local_dir, "workspace", collection, add_opts)
    return f"cask:{root}"


def resolve_targets(target, opts={}):
    """Resolve any accepted target to the item(s) it covers.

    :param str|digtk.lib.items.ItemLocation target: An ARK (one bronze item), a
        bronze item folder (one bronze item, resolved by path — see
        items.resolve_path()), a 'cask:/workspace/...' path (every item at or
        under it — see items.list_workspace_items()), a local directory
        (staged first, see stage(), then treated like its workspace path), or
        an ItemLocation.
    :param dict opts: Forwarded to stage() for a local directory.
    :returns: Items, in path order.
    :rtype: list[digtk.lib.items.ItemLocation]
    :raises ValueError: If `target` is none of the above.
    :raises LookupError: If a workspace path holds no files at all.
    """
    if isinstance(target, items.ItemLocation):
        return [target]
    if target.startswith("ark:/"):
        return [items.resolve(target)]
    if not target.startswith("cask:"):
        if os.path.isdir(target):
            target = stage(target, opts)
        else:
            try:
                return [items.resolve_path(target)]
            except ValueError:
                raise ValueError(
                    f"expected an ARK, a bronze path, a cask:/workspace/... path, or an "
                    f"existing local folder, got {target!r}"
                )
    item_dirs = items.list_workspace_items(target)
    if not item_dirs:
        raise LookupError(f"no files found at or under {target}")
    return [items.resolve_workspace(path) for path in item_dirs]


def apply_to_targets(target, fn, opts={}):
    """Run one tool function over every item a target covers — the shared body
    of the root-level tool commands (`digtk ocr tesseract`, `digtk accessibility
    alt-text|tag-pdf`, `digtk split-spreads detect|apply`).

    :param str target: See resolve_targets().
    :param callable fn: The tool's model function, called as fn(item, fn_opts).
    :param dict opts: 'stage' (str, required — reporting stage name), 'fn_opts'
        (dict, forwarded to fn), 'reporting_job_id' / 'reporting_collection' (see
        digtk.lib.reporting.track() — one reporting.command row per item), plus
        stage()'s opts for a local-folder target.
    :returns: fn's own result when the target is a single item (an ARK, or a
        workspace folder holding one item), else {"status": "success"|"partial",
        "items": [fn result, ...]} — "partial" when any item's own result is
        (some pages failed; see e.g. digtk.models.ocr.run_ocr()).
    :rtype: dict
    """
    fn_opts = opts.get("fn_opts") or {}
    results = []
    for item in resolve_targets(target, opts):
        label = items.item_id(item)
        utils.log(f"{opts['stage']}: {label}")
        with reporting.track(
            opts.get("reporting_job_id"), stage=opts["stage"], item_id=label,
            collection=opts.get("reporting_collection"), options=fn_opts,
        ):
            results.append(fn(item, fn_opts))
    if len(results) == 1:
        return results[0]
    return {"status": _overall_status(results), "items": results}


def _overall_status(results):
    """
    :param list[dict] results: Tool results.
    :returns: "partial" if any result reports partial success, else "success".
    :rtype: str
    """
    return "partial" if any(r.get("status") == "partial" for r in results) else "success"


def list_collections(opts={}):
    """List workspace collections (anything with a source/ directory).

    :param dict opts: 'username' (str) — only this user's collections.
    :returns: [{"username": str, "collection": str,
        "key": "workspace/username/collection" (Dagster's workspace partition key),
        "path": "cask:/workspace/username/collection"}], sorted by key.
    :rtype: list[dict]
    """
    base = f"cask:{items.WORKSPACE_PREFIX.rstrip('/')}"
    if opts.get("username"):
        usernames = [opts["username"]]
    else:
        usernames = [e.name for e in fs.ls(base, _LS_OPTS) if e.kind == "directory"]

    found = []
    for username in usernames:
        for entry in fs.ls(f"{base}/{username}", _LS_OPTS):
            if entry.kind != "directory":
                continue
            path = f"{base}/{username}/{entry.name}"
            children = fs.ls(path, _LS_OPTS)
            if any(c.kind == "directory" and c.name == items.WORKSPACE_SOURCE_DIR for c in children):
                found.append({
                    "username": username, "collection": entry.name,
                    "key": f"workspace/{username}/{entry.name}", "path": path,
                })
    return sorted(found, key=lambda c: c["key"])


def list_items(path):
    """List every item folder under a workspace path, with its processable files
    — what Dagster's process_workspace_job fans out over (one step per file).

    :param str path: Workspace collection root or any directory under its source/.
    :returns: [{"item": "cask:/workspace/.../source[/...]", "files": [image/PDF
        filenames, in item order]}], one per item folder (see
        items.list_workspace_items()).
    :rtype: list[dict]
    """
    found = []
    for item_dir in items.list_workspace_items(path):
        item = items.resolve_workspace(item_dir)
        image_paths, pdf_paths = prep.partition_by_format(item.bronze_binary_paths)
        wanted = set(image_paths) | set(pdf_paths)
        found.append({
            "item": item_dir,
            "files": [p.rsplit("/", 1)[-1] for p in item.bronze_binary_paths if p in wanted],
        })
    return found


def _formats(item, files=None):
    """
    :param digtk.lib.items.ItemLocation item: Resolved item location.
    :param set[str] files: Only consider these source filenames, if given.
    :returns: Which of {"image", "pdf"} the item (or those of its files) has binaries of.
    :rtype: set[str]
    """
    paths = [p for p in item.bronze_binary_paths if not files or p.rsplit("/", 1)[-1] in files]
    image_paths, pdf_paths = prep.partition_by_format(paths)
    return ({"image"} if image_paths else set()) | ({"pdf"} if pdf_paths else set())


def run(target, opts={}):
    """Run a chosen set of tool steps over every item a target covers.

    Steps always run in STEP_ORDER, whatever order they're given in. A step
    whose formats an item doesn't have (e.g. tag-pdf on an images-only folder)
    is recorded as skipped for that item rather than failing the whole run. A
    step that raises (e.g. split-detect during a Samwise outage) doesn't stop
    the item's remaining steps either: its error is recorded (and logged via
    utils.warn(), and as a failed reporting row), the next step runs, and the
    overall status is "partial". Only when every step that ran failed, across
    every item, does run() itself raise — so a total outage still exits non-zero.
    Note split-detect and split-apply are deliberately separate: running both
    in one go skips the human review of gutters.json that sits between them.

    :param str|digtk.lib.items.ItemLocation target: See resolve_targets().
    :param dict opts: 'steps' (list[str], required — keys of STEPS),
        'step_opts' ({step: dict} — forwarded to that step's function),
        'files' (list[str], optional — only process these source filenames of
        each item; folder-level steps (FOLDER_STEPS) are skipped, and output
        naming still follows the whole item, so a per-file run writes the same
        paths a whole-item run would), 'reporting_job_id' /
        'reporting_collection' (see digtk.lib.reporting.track() — each
        item/step pair, or item#file/step with 'files', gets its own
        reporting.command row), plus stage()'s opts for a local-folder target.
    :returns: {"status": "success"|"partial", "items": [{"item": str, "ark": str|None,
        "steps": {step: step result | {"skipped": reason} | {"status": "error",
        "error": str}}}]} — "partial" when any step failed, or any step's own
        result is partial (some pages failed).
    :rtype: dict
    :raises ValueError: If no steps, or an unknown step, is given.
    :raises RuntimeError: If every step that ran failed.
    """
    steps = opts.get("steps") or []
    unknown = [s for s in steps if s not in STEPS]
    if not steps or unknown:
        raise ValueError(f"steps must be a non-empty subset of {STEP_ORDER}, got {steps}")
    ordered = [s for s in STEP_ORDER if s in steps]
    step_opts = opts.get("step_opts") or {}
    files = set(opts.get("files") or [])

    results = []
    for item in resolve_targets(target, opts):
        label = items.item_id(item)
        if files:
            label = f"{label}#{','.join(sorted(files))}"
        formats = _formats(item, files)
        item_result = {"item": label, "ark": item.ark, "steps": {}}
        for step in ordered:
            fn, needs = STEPS[step]
            if files and step in FOLDER_STEPS:
                item_result["steps"][step] = {"skipped": "split-spreads runs per folder, not per file"}
                continue
            if not formats & needs:
                item_result["steps"][step] = {"skipped": f"no {'/'.join(sorted(needs))} binaries"}
                continue
            this_opts = {**step_opts.get(step, {}), **({"files": files} if files else {})}
            utils.log(f"{label}: {step}")
            try:
                with reporting.track(
                    opts.get("reporting_job_id"), stage=step, item_id=label,
                    collection=opts.get("reporting_collection"), options=step_opts.get(step, {}),
                ):
                    item_result["steps"][step] = fn(item, this_opts)
            except Exception as e:  # noqa: BLE001 — one step failing shouldn't stop the item's other steps
                utils.warn(f"{label}: {step} failed, continuing with remaining steps: {e}")
                item_result["steps"][step] = {"status": "error", "error": str(e)}
        results.append(item_result)

    step_results = [r for item in results for r in item["steps"].values() if "skipped" not in r]
    errors = [r for r in step_results if r.get("status") == "error"]
    if step_results and len(errors) == len(step_results):
        raise RuntimeError(f"every step failed ({len(errors)}): {errors[0]['error']}")
    status = "partial" if errors else _overall_status(step_results)
    return {"status": status, "items": results}


def pull(path, local_dir, opts={}):
    """Download every file at or under a CaskFS directory to local disk,
    preserving the relative layout — e.g. an item's split-spreads/ review
    sheets and gutters.json, for a human to check.

    :param str path: 'cask:/...' directory.
    :param str local_dir: Local destination directory (created if missing).
    :param dict opts: Unused.
    :returns: {"status": "success", "source": str, "dest": str, "files": [local paths]}
    :rtype: dict
    """
    root = path.rstrip("/")
    written = []
    stack = [root]
    while stack:
        current = stack.pop()
        for entry in fs.ls(current, _LS_OPTS):
            cask_path = f"cask:{entry.path}"
            if entry.kind == "directory":
                stack.append(cask_path)
                continue
            rel = cask_path[len(root):].lstrip("/")
            dest = os.path.join(local_dir, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            fs.write(dest, src=cask_path, opts={"replace": True})
            written.append(dest)
    return {"status": "success", "source": root, "dest": local_dir, "files": sorted(written)}


# --- promote ------------------------------------------------------------------

# Lineage relation for each promoted output, by (tool dir, filename suffix) —
# the same relations each tool records when it writes the file itself.
_RELATIONS = [
    (ocr.TEXT_DIR, "ocr.hocr", ocr.HOCR_LINK_RELATION),
    (ocr.TEXT_DIR, "ocr-result.json", ocr.HOCR_LINK_RELATION),
    (ocr.TEXT_DIR, "transcript.txt", ocr.TRANSCRIPT_LINK_RELATION),
    ("accessibility", "alt-text.json", accessibility.ALT_TEXT_LINK_RELATION),
    ("accessibility", "layout.json", accessibility.LAYOUT_LINK_RELATION),
    ("accessibility", "tagged.pdf", accessibility.TAGGED_PDF_LINK_RELATION),
    ("split-spreads", "gutters.json", split.GUTTERS_LINK_RELATION),
]


def _relation_for(rel_path):
    """Lineage relation for one promoted output, from its path under the item's
    derivatives/ root.

    :param str rel_path: e.g. 'text/ocr.hocr', 'split-spreads/pages/a_L.tif'.
    :returns: Relation IRI, or None (CaskFS's schema:source default).
    :rtype: str|None
    """
    segments = rel_path.split("/")
    if "split-spreads" in segments:
        if "pages" in segments:
            return split.SPLIT_PAGE_LINK_RELATION
        if "review" in segments or "review-sheets" in segments:
            return split.REVIEW_LINK_RELATION
    for tool_dir, suffix, relation in _RELATIONS:
        if tool_dir in segments and rel_path.endswith(suffix):
            return relation
    return None


def _sources_for(rel_path, bronze_by_filename):
    """Which bronze binaries one promoted output was derived from.

    Every tool names its per-source outputs after the source binary — a
    '{stem}-' filename prefix (multi-image items), a '{filename}' or '{stem}'
    path segment (per-PDF dirs), or '{stem}_L'/'{stem}_R'/the filename itself
    (split pages). Anything that doesn't name one source (e.g. gutters.json, a
    single-binary item's plain ocr.hocr) is linked to every binary.

    :param str rel_path: Path under the item's derivatives/ root.
    :param dict bronze_by_filename: {filename: bronze cask path}.
    :rtype: list[str]
    """
    segments = rel_path.split("/")
    name = segments[-1]
    matches = []
    for filename, bronze_path in bronze_by_filename.items():
        stem = os.path.splitext(filename)[0]
        if (filename in segments or stem in segments[:-1] or name.startswith(f"{stem}-")
                or name.startswith(f"{stem}_L") or name.startswith(f"{stem}_R")):
            matches.append(bronze_path)
    return matches or sorted(bronze_by_filename.values())


def _list_outputs(item):
    """Every tool output file for one workspace item.

    Only descends into the tool directories (and per-PDF stem directories) —
    an item's derivatives/ root can also hold a nested sub-item's own
    derivatives (a folder that has both files and subfolders).

    :param digtk.lib.items.ItemLocation item: Workspace item.
    :returns: [(rel_path under the item's derivatives root, cask path)], sorted.
    :rtype: list[tuple[str, str]]
    """
    root = f"cask:{derivative.dest_path(type='workspace', collection=item.collection, rel_path=items.dest_rel(item), opts=items.dest_opts(item))}"
    stems = {os.path.splitext(p.rsplit("/", 1)[-1])[0] for p in item.bronze_binary_paths}
    try:
        top = fs.ls(root, _LS_OPTS)
    except FileNotFoundError:
        return []

    outputs = []
    stack = [f"cask:{e.path}" for e in top if e.kind == "directory" and (e.name in _TOOL_DIRS or e.name in stems)]
    while stack:
        current = stack.pop()
        for entry in fs.ls(current, _LS_OPTS):
            cask_path = f"cask:{entry.path}"
            if entry.kind == "directory":
                stack.append(cask_path)
            else:
                outputs.append((cask_path[len(root) + 1:], cask_path))
    return sorted(outputs)


def promote(path, ark, opts={}):
    """Copy a reviewed workspace item's outputs into the ARK's silver location.

    For when a folder was processed in workspace first and has since been
    ingested to bronze under an ARK: every tool output (text/, accessibility/,
    split-spreads/) is copied as-is — no OCR/vision calls are re-run — to the
    same relative path under the ARK's silver item location, with lineage
    re-pointed at the matching bronze binaries (matched by filename; see
    _sources_for()) and each output's own relation (see _relation_for()).

    Outputs generated in workspace had no bronze sidecar to draw on, so e.g.
    alt text wasn't prompted with the item's catalog metadata — re-run that step
    on the ARK instead if that matters for a collection.

    :param str path: Workspace item directory ('cask:/workspace/.../source[/...]').
    :param str ark: The item's ARK, once ingested.
    :param dict opts: 'replace' (bool, default False — overwrite outputs already
        in silver; by default any conflict aborts before anything is written),
        'dry_run' (bool, default False — report what would be copied).
    :returns: {"status": "success", "ark": str, "source": str, "dry_run": bool,
        "promoted": [{"from": str, "to": str}]}
    :rtype: dict
    :raises LookupError: If the workspace item has no outputs, or the ARK doesn't resolve.
    :raises ValueError: If a workspace source file has no same-named bronze
        binary, or (without 'replace') outputs already exist in silver.
    """
    ws_item = items.resolve_workspace(path)
    bronze_item = items.resolve(ark)

    bronze_by_filename = {p.rsplit("/", 1)[-1]: p for p in bronze_item.bronze_binary_paths}
    missing = sorted(
        p.rsplit("/", 1)[-1] for p in ws_item.bronze_binary_paths
        if p.rsplit("/", 1)[-1] not in bronze_by_filename
    )
    if missing:
        raise ValueError(f"these workspace source files have no same-named bronze binary under {ark}: {missing}")

    outputs = _list_outputs(ws_item)
    if not outputs:
        raise LookupError(f"no tool outputs found for {items.item_id(ws_item)}")

    plan = []
    for rel_path, cask_path in outputs:
        dest = derivative.dest_path(
            type="silver", collection=bronze_item.collection,
            rel_path=items.dest_rel(bronze_item, rel_path), opts=items.dest_opts(bronze_item),
        )
        plan.append((rel_path, cask_path, f"cask:{dest}"))

    replace = opts.get("replace", False)
    if not replace:
        conflicts = [dest for _, _, dest in plan if fs.exists(dest)]
        if conflicts:
            raise ValueError(f"{len(conflicts)} output(s) already exist in silver (pass replace to overwrite): {conflicts}")

    promoted = [{"from": cask_path, "to": dest} for _, cask_path, dest in plan]
    if opts.get("dry_run"):
        return {"status": "success", "ark": ark, "source": items.item_id(ws_item), "dry_run": True, "promoted": promoted}

    for rel_path, cask_path, _ in plan:
        write_opts = {**items.dest_opts(bronze_item), "replace": replace}
        relation = _relation_for(rel_path)
        if relation:
            write_opts["link_relation"] = relation
        derivative.write(
            type="silver", collection=bronze_item.collection,
            rel_path=items.dest_rel(bronze_item, rel_path), data=fs.read(cask_path),
            source_files=_sources_for(rel_path, bronze_by_filename), opts=write_opts,
        )
    return {"status": "success", "ark": ark, "source": items.item_id(ws_item), "dry_run": False, "promoted": promoted}
