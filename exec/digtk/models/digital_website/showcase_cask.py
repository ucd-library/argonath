"""
Seeds dc-showcase's gold files (home page hero banner and browse by images,
with their json) into CaskFS. dc-showcase owns the files; they're mounted from
its repo (CONFIG["showcase_cask"]["init_dir"]), mirroring showcase_db.init_db().
"""
from pathlib import Path

from digtk.lib import fs
from digtk.lib.config import CONFIG


def init_cask(opts={}):
    """Copies each file under the init dir to the same relative path in the
    gold project, skipping files already in CaskFS so later edits are kept.

    :param dict opts: 'init_dir' (str, overrides the config); 'project'
        (str, gold project, default CONFIG["showcase"]["gold_project"]).
    :returns: {"added": [cask paths], "skipped": [cask paths]}.
    :rtype: dict
    :raises FileNotFoundError: If the init dir has no files.
    """
    init_dir = Path(opts.get("init_dir") or CONFIG["showcase_cask"]["init_dir"])
    project = opts.get("project") or CONFIG["showcase"]["gold_project"]
    files = sorted(p for p in init_dir.rglob("*") if p.is_file() and not p.name.startswith("."))
    if not files:
        raise FileNotFoundError(f"no files in {init_dir}")

    result = {"added": [], "skipped": []}
    for path in files:
        dest = f"cask:/gold/{project}/{path.relative_to(init_dir).as_posix()}"
        if fs.exists(dest):
            result["skipped"].append(dest)
            continue
        fs.write(dest, src=str(path))
        result["added"].append(dest)
    return result
