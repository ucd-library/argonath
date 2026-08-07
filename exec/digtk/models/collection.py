"""
Collection creation — add a local file or directory as a new CaskFS collection.

This module only formulates *where* an upload goes: a destination root ('bronze' or
'workspace' — the only two roots new collections may be added under, per
docs/cask-conventions.md) plus group/collection/time inputs resolved into a CaskFS
path. The actual transfer is delegated to the `cask cp` CLI, which owns its own
rsync-style batch hash-check sync, progress reporting, and (by default) an interactive
confirmation prompt before copying — this module never touches file bytes itself.

Talking to the right CaskFS instance is the cask CLI's own concern too: `opts['env']`
is passed straight through as `cask cp`'s `-e/--env` flag (see `cask env` in the
caskfs repo), so environment selection follows whatever the caller has configured
there rather than digtk's own CASK_API_URL/CASK_API_KEY (used by digtk.lib.fs.cask's
HTTP backend, a separate connection path).
"""
import os
import subprocess
from datetime import datetime, timezone

CURRENT_TIME = "CURRENT_TIME"
CURRENT_DATE = "CURRENT_DATE"

ROOTS = ("bronze", "workspace")


def resolve_time(time):
    """Resolve a bronze time-folder value, expanding the CURRENT_TIME/CURRENT_DATE sentinels.

    :param str|None time: A literal time-folder name, one of the CURRENT_TIME/CURRENT_DATE
        sentinels, or None.
    :returns: Resolved time-folder name, or None if `time` was None.
    :rtype: str|None
    """
    if time == CURRENT_TIME:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if time == CURRENT_DATE:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return time


def dest_path(root, collection, opts={}):
    """Build the CaskFS-absolute destination path for a new collection.

    :param str root: 'bronze' or 'workspace' — the only roots new collections may be added under.
    :param str collection: Collection name.
    :param dict opts: 'group' (required when root='bronze'), 'time' (bronze only — literal
        time-folder name or a CURRENT_TIME/CURRENT_DATE sentinel; defaults to 'latest' if
        omitted), 'username' (workspace only, defaults to the current OS user).
    :returns: CaskFS-absolute path, e.g. '/bronze/digital-collections/ucd-photos/latest'.
    :rtype: str
    :raises ValueError: If `root` isn't 'bronze'/'workspace', a required opt is missing, or
        'time' is given for root='workspace' (workspace has no time dimension).
    """
    if root not in ROOTS:
        raise ValueError(f"root must be one of {ROOTS}, got {root!r}")

    if root == "bronze":
        group = opts.get("group")
        if not group:
            raise ValueError("opts['group'] is required when root='bronze'")
        time = resolve_time(opts.get("time")) or "latest"
        return f"/bronze/{group}/{collection}/{time}"

    if opts.get("time"):
        raise ValueError("opts['time'] does not apply when root='workspace' (no time dimension)")
    username = opts.get("username") or os.environ.get("USER") or os.environ.get("USERNAME")
    if not username:
        raise ValueError("opts['username'] is required when root='workspace' (could not detect current user)")
    return f"/workspace/{username}/{collection}"


def _build_argv(source, dest, opts):
    """Build the `cask cp` argv for uploading a collection.

    `cask cp`'s local-to-CaskFS path only checks its *source* argument for a 'cask:'
    prefix; a local source is copied to `dest-path` taken as a literal CaskFS path,
    so `dest` must NOT be 'cask:'-prefixed here (unlike a cask:->cask: or cask:->local
    cask cp invocation, which this module never issues).

    :param str source: Local path (file or directory) to upload.
    :param str dest: CaskFS-absolute destination path (no 'cask:' prefix).
    :param dict opts: 'env' (cask CLI environment name, -> -e), 'replace' (bool, -> -x),
        'dry_run' (bool, -> -d), 'yes' (bool, default False, -> -y — skip cask cp's
        confirmation prompt; by default cask cp prompts the user before copying).
    :returns: argv list suitable for subprocess.run.
    :rtype: list[str]
    """
    argv = ["cask", "cp", source, dest]
    if opts.get("env"):
        argv += ["-e", opts["env"]]
    if opts.get("replace"):
        argv.append("-x")
    if opts.get("dry_run"):
        argv.append("-d")
    if opts.get("yes"):
        argv.append("-y")
    return argv


def add(source, root, collection, opts={}):
    """Add a local file or directory to CaskFS as a new (or extended) collection.

    Formulates the destination path from `root`/`collection`/opts, then shells out to
    `cask cp` to perform the upload — `cask cp` does its own hash-based batch sync
    against what's already in CaskFS, so re-running `add()` over a source that's
    partially already uploaded only transfers what's changed.

    By default `cask cp` is left to prompt the user (on stdin) to confirm before
    copying, same as running it by hand — pass opts['yes']=True to skip that prompt
    for non-interactive/scripted use.

    :param str source: Local path (file or directory) to upload.
    :param str root: 'bronze' or 'workspace' — the only roots new collections may be added under.
    :param str collection: Collection name.
    :param dict opts: 'group' (required when root='bronze'), 'time' (bronze only, see
        dest_path()), 'username' (workspace only, see dest_path()), 'env' (cask CLI
        environment name), 'replace' (bool), 'dry_run' (bool), 'yes' (bool, default False
        — skip cask cp's confirmation prompt), 'run' (callable, defaults to
        subprocess.run — injectable for tests).
    :returns: Completed cask cp invocation.
    :rtype: subprocess.CompletedProcess
    :raises ValueError: If `root` is invalid or a required opt is missing.
    :raises FileNotFoundError: If `source` does not exist.
    :raises subprocess.CalledProcessError: If the cask cp invocation exits non-zero.
    """
    if not os.path.exists(source):
        raise FileNotFoundError(source)

    dest = dest_path(root, collection, opts)
    argv = _build_argv(source, dest, opts)
    run = opts.get("run", subprocess.run)
    return run(argv, check=True)
