"""
Batching and naming helpers shared by the fan-out jobs (lib/pipeline_fanout.py's
showcase_process_collection_job, lib/pipeline_workspace.py's workspace_process_job).

Both jobs fan out one Dagster step per file (or per item folder). A large
collection can hold tens of thousands of files, which is more steps than one
run handles well, so each fan-out caps itself at `max_steps` (a launchpad
setting): past that, several tasks share one step, run one after another inside
it, and still report one result per task in the Events screen.

Every task carries an `id`: its CaskFS path relative to the run's partition
(e.g. "latest/tree/mv-rapleytrail-formatted/Preservation/page_003.tif" under
partition "bronze/preservica/haeger"), so a step name or event traces straight
back to the file in CaskFS. Step names (Dagster mapping keys) only allow
[A-Za-z0-9_] and must stay under the filesystem's filename limit (Dagster's IO
manager names a file after each), so step_key() sanitizes and caps them;
events keep the real paths, and each batch step's full file list.
"""
import hashlib
import math
import re

from dagster import AssetMaterialization, MetadataValue

DEFAULT_MAX_STEPS = 500


def relative_id(path, partition_key):
    """A CaskFS path relative to the run's partition.

    :param str path: 'cask:/...' path (or a bare '/...' / relative path).
    :param str partition_key: e.g. "bronze/preservica/haeger" or "workspace/jdoe/box-12".
    :returns: e.g. "latest/tree/x/Preservation/page_003.tif"; a path outside the
        partition comes back unchanged apart from its 'cask:/' prefix.
    :rtype: str
    """
    raw = path[len("cask:"):] if path.startswith("cask:") else path
    raw = raw.lstrip("/")
    prefix = f"{partition_key.strip('/')}/"
    return raw[len(prefix):] if raw.startswith(prefix) else raw


def batches(tasks, max_steps):
    """Split tasks into at most `max_steps` consecutive groups.

    :param list tasks: Fan-out tasks, in order (one folder's files stay adjacent).
    :param int max_steps: The cap; 0 (or less) never batches.
    :returns: Lists of tasks — one task each when no batching is needed.
    :rtype: list[list]
    """
    if max_steps <= 0 or len(tasks) <= max_steps:
        return [[t] for t in tasks]
    size = math.ceil(len(tasks) / max_steps)
    return [tasks[i:i + size] for i in range(0, len(tasks), size)]


# Longest mapping key step_key() produces. Dagster's filesystem IO manager
# stores each mapped step's input in a file named after its mapping key, so a
# key must fit the OS's 255-byte filename limit (with room to spare).
MAX_KEY_LENGTH = 200


def step_key(ids, used):
    """Dagster mapping key for one (possibly batched) step: the task ids,
    sanitized to [A-Za-z0-9_] and joined with '__'.

    Capped at MAX_KEY_LENGTH: once the next id wouldn't fit, the rest are
    summarized as '__and_<N>_more__<hash>' (a short hash of every id, so two
    batches that start alike still get distinct keys) — the full list is in the
    step's own "batch of N" event (see log_batch()). Two keys that still come
    out identical (e.g. ids 'a-b' and 'a_b') get a '__2', '__3'... suffix.

    :param list[str] ids: The step's task ids.
    :param set used: Keys already yielded in this fan-out (updated in place).
    :rtype: str
    """
    parts = [re.sub(r"[^A-Za-z0-9_]", "_", i) for i in ids] or ["empty"]
    key = "__".join(parts)
    if len(key) > MAX_KEY_LENGTH:
        digest = hashlib.sha1("\n".join(ids).encode()).hexdigest()[:8]
        kept = []
        for part in parts:
            remaining = len(parts) - len(kept) - 1
            suffix = f"__and_{remaining}_more__{digest}" if remaining else f"__{digest}"
            if len("__".join(kept + [part])) + len(suffix) > MAX_KEY_LENGTH:
                break
            kept.append(part)
        remaining = len(parts) - len(kept)
        if not kept:
            # A single id longer than the cap: keep its tail (the file name end).
            head = parts[0][-(MAX_KEY_LENGTH - 40):].lstrip("_")
            kept, remaining = [head], len(parts) - 1
        key = "__".join(kept) + (f"__and_{remaining}_more" if remaining else "") + f"__{digest}"
    candidate, n = key, 1
    while candidate in used:
        n += 1
        candidate = f"{key}__{n}"
    used.add(candidate)
    return candidate


def log_batching(context, kind, n_tasks, grouped, max_steps):
    """Record one fan-out's batching decision in the run's Events.

    :param context: Dagster op execution context.
    :param str kind: What's being fanned out (e.g. "files", "finalize").
    :param int n_tasks: Tasks before batching.
    :param list[list] grouped: batches()'s result.
    :param int max_steps: The cap in effect.
    """
    if len(grouped) == n_tasks:
        context.log.info(f"{kind}: {n_tasks} step(s), one per task (max_steps {max_steps or 'off'})")
        return
    largest = max(len(b) for b in grouped)
    context.log.info(
        f"{kind}: {n_tasks} task(s) > max_steps {max_steps} -> batched into "
        f"{len(grouped)} step(s) of up to {largest}"
    )


def log_batch(context, ids):
    """Record which tasks one (batched) step covers, at its start.

    :param context: Dagster op execution context.
    :param list[str] ids: The step's task ids.
    """
    if len(ids) > 1:
        context.log.info(f"batch of {len(ids)}: {', '.join(ids)}")


def emit(context, stage, item_id, result, status="success", partition=None, **extra):
    """Log an AssetMaterialization carrying per-item stage metadata for
    traceability -- the per-item progress signal this job uses in place of
    Dagster partition status (see lib.pipeline_fanout). The authoritative
    success/failure record is digtk.lib.reporting's Postgres row, written by
    the digtk command itself; this is a Dagster-UI-visible complement to it.

    :param context: Dagster op execution context.
    :param str stage: Pipeline stage name (e.g. "derivatives").
    :param str item_id: What the event is about — a file/item path relative to
        the partition.
    :param result: digtk's parsed JSON result, or None on failure.
    :param str status: "success" or "failed".
    :param str|None partition: Event partition (a collection key); defaults to
        the run's partition key, or none for an unpartitioned run.
    :param extra: Additional string-coercible metadata (e.g. error=...).
    """
    if partition is None and context.has_partition_key:
        partition = context.partition_key
    metadata = {
        "item_id": MetadataValue.text(item_id),
        "stage": MetadataValue.text(stage),
        "status": MetadataValue.text(status),
        **{k: MetadataValue.text(str(v)) for k, v in extra.items()},
    }
    if status == "failed":
        context.log.error(f"[{item_id}] {stage}: FAILED -> {extra.get('error')}")
    else:
        context.log.info(f"[{item_id}] {stage}: ok -> {result}")
    context.log_event(AssetMaterialization(
        asset_key=f"argonath_fanout_{stage}",
        partition=partition,
        metadata=metadata,
    ))
