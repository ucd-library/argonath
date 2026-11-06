"""
Workspace staging for the root-level tools (ocr, accessibility, split-spreads)
— running them on scans before they have an ARK or a bronze copy.

Layout, per docs/cask-conventions.md's workspace section:

  /workspace/[username]/[folder]/
    source/        staged source binaries (a local folder's contents, as-is)
    derivatives/   every tool output, mirroring source/'s item paths

An item is every file sitting directly in one source/ directory (see
digtk.lib.items.resolve_workspace()): a flat folder of scans is one item, a
nested box/folder layout is one item per folder that holds files.

This module owns the parts of that workflow that aren't plain resolution:
staging a local folder in (stage(), via digtk.models.collection.add()'s
`cask cp`), turning any accepted target into its items (resolve_targets()),
running a chosen set of tool steps over them (run()), and pulling outputs back to
local disk for review (pull()).
"""
import os

from digtk.lib import fs, items, reporting, utils
from digtk.models import accessibility, figures, ocr, split
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
    "ocr-correct": (ocr.correct_ocr, {"image", "pdf"}),
    "alt-text": (accessibility.generate_alt_text, {"image", "pdf"}),
    "figures": (accessibility.detect_figures, {"image", "pdf"}),
    "pdf-build": (accessibility.build_pdf, {"image"}),
    "tag-pdf": (accessibility.tag_pdf, {"pdf"}),
}
STEP_ORDER = list(STEPS)

# Steps that work across a whole item folder rather than file by file —
# split-spreads keeps one gutters.json and one set of contact sheets per item,
# so it can't be split into one run per file (see run()'s 'files' opt).
FOLDER_STEPS = {"split-detect", "split-apply", "pdf-build"}


def stage(local_dir, opts={}):
    """Upload a local folder into a workspace folder's source/ directory.

    Re-running over the same folder only transfers what's changed (`cask cp`
    does its own hash-based sync — see digtk.models.collection.add()).

    :param str local_dir: Local directory to upload.
    :param dict opts: 'folder' (str, default the local folder's own name),
        'username' (str, default the current OS user), 'yes' (bool, default True
        — skip cask cp's confirmation prompt), 'env' (cask CLI environment),
        'replace' (bool), 'run' (injectable subprocess.run, for tests).
    :returns: The workspace folder root, e.g. 'cask:/workspace/jdoe/box-12'.
    :rtype: str
    :raises ValueError: If `local_dir` isn't a directory.
    :raises subprocess.CalledProcessError: If cask cp fails.
    """
    if not os.path.isdir(local_dir):
        raise ValueError(f"not a local directory: {local_dir}")
    folder = opts.get("folder") or os.path.basename(os.path.abspath(local_dir))
    add_opts = {
        "username": opts.get("username"),
        "subdir": items.WORKSPACE_SOURCE_DIR,
        "yes": opts.get("yes", True),
        "env": opts.get("env"),
        "replace": opts.get("replace", False),
    }
    if opts.get("run"):
        add_opts["run"] = opts["run"]

    root = collection_model.dest_path("workspace", folder, {"username": add_opts["username"]})
    utils.log(f"Staging {local_dir} -> cask:{root}/{items.WORKSPACE_SOURCE_DIR}")
    collection_model.add(local_dir, "workspace", folder, add_opts)
    return f"cask:{root}"


def resolve_targets(target, opts={}):
    """Resolve any accepted target to the item(s) it covers.

    :param str|digtk.lib.items.ItemLocation target: An ARK (one bronze item), a
        bronze item folder, with or without 'cask:' (one bronze item, resolved by path — see
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
    if target.startswith("cask:/bronze/"):
        return [items.resolve_path(target)]
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


def list_folders(opts={}):
    """List workspace folders (anything with a source/ directory).

    :param dict opts: 'username' (str) — only this user's folders.
    :returns: [{"username": str, "folder": str,
        "key": "workspace/username/folder" (Dagster's workspace partition key),
        "path": "cask:/workspace/username/folder"}], sorted by key.
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
                    "username": username, "folder": entry.name,
                    "key": f"workspace/{username}/{entry.name}", "path": path,
                })
    return sorted(found, key=lambda c: c["key"])


def list_items(path):
    """List every item folder under a workspace path, with its processable files
    — what Dagster's workspace_process fans out over (one step per file).

    :param str path: Workspace folder root or any directory under its source/.
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

