"""
digtk preservica — pull a collection out of Preservica into bronze.

Subcommands:
  config  — show the current Preservica connection and settings (nothing is set
            by this command — see its help for how to actually set values)
  token   — print a live access token (and, given a URL, a ready-to-run curl
            command) for manually replaying a request outside the client's
            retry logic
  list    — browse a Preservica structural object's children (or the collection
            root), to discover the ref to pass to `import` (calls digtk.lib.preservica
            directly — no bronze/collection business logic involved)
  import  — walk a Preservica collection into bronze's by-ref/ layout, always
            registering the collection along the way (see
            digtk.models.collection.preservica_import/register_collection)
  sync    — sync only specific changed entities (folders/items) for an
            already-registered collection, never a full re-walk (see
            digtk.models.collection.preservica_sync_entities) — used by
            Dagster's reconcile_collection_items
  tree    — rebuild bronze's tree/ layout from an already-imported by-ref/
            (see digtk.models.collection.preservica_tree)
  changes — list registered Preservica collections with any change since a
            given timestamp (see digtk.models.collection.find_changed_preservica_collections)
            — used by Dagster's preservica_auto_sync_sensor
"""
from typing import Annotated

import typer

from digtk.lib import preservica, utils
from digtk.lib.preservica import PreservicaAuthError, PreservicaRequestError
from digtk.models import collection as collection_model

app = typer.Typer(help="Pull a collection out of Preservica into bronze.", no_args_is_help=True)

_TYPE_LABEL = {"SO": "folder", "IO": "item"}


@app.command("config")
def show_config():
    """Show the current Preservica connection and settings — this command only
    displays values, it doesn't set anything.

    Every value shown here (tenant/username/password/base_url, and the request
    timeout/concurrency/page-size/token-refresh-buffer tuning knobs) is a plain
    digtk dotpath setting: persist a new value with
    `digtk config set preservica.<name> <value>` (password prompts for hidden
    input, same as any other secret — see `digtk config show` for every dotpath
    across all of digtk, not just Preservica's)."""
    settings = preservica.settings_summary()
    for dotpath in sorted(settings):
        utils.log(f"{dotpath} = {settings[dotpath]}")

    utils.result({"status": "success", "settings": settings})


@app.command("token")
def token(
    url: Annotated[str | None, typer.Argument(
        help="Optional full Preservica URL (e.g. a failing bitstream /content "
        "URL) to build a ready-to-run curl command for.",
    )] = None,
):
    """Print a live Preservica access token (and, given a URL, a copy-pasteable
    curl command) for manually replaying a request outside the client's retry
    logic — e.g. to inspect the raw response on a 502."""
    try:
        client = preservica.build_client()
        client.login()
    except ValueError as e:
        utils.error(str(e))
    except (PreservicaAuthError, PreservicaRequestError) as e:
        utils.error(str(e))

    utils.log(f"Base URL: {client.base_url}")
    utils.log(f"Token: {client.token}")
    utils.log(f"Valid for: {client.valid_for_s // 60} minute(s)")
    if url:
        utils.log("")
        utils.log(
            f"curl -sD - -o /tmp/preservica-download.bin -w '\\nHTTP %{{http_code}} in %{{time_total}}s\\n' "
            f"-H 'Preservica-Access-Token: {client.token}' '{url}'"
        )

    utils.result({
        "status": "success",
        "base_url": client.base_url,
        "token": client.token,
        "valid_for_s": client.valid_for_s,
    })


@app.command("list")
def list_children(
    ref: Annotated[str | None, typer.Argument(
        help="Structural object ref to list children of. Omit to list the collection root.",
    )] = None,
):
    """Browse the children (folders/items) of a Preservica structural object, or
    the collection root if no ref is given — use this to discover the ref to pass
    to `import`."""
    try:
        client = preservica.build_client()
        children = client.get_children(ref)
    except ValueError as e:
        utils.error(str(e))
    except (PreservicaAuthError, PreservicaRequestError) as e:
        utils.error(str(e))

    if not children:
        utils.log("(no children)")
    for child in children:
        label = _TYPE_LABEL.get(child["type"], child["type"]).ljust(6)
        utils.log(f"{label}  {child['ref']}  {child['title']}")

    utils.result({"status": "success", "ref": ref, "children": children})


@app.command("import")
def import_(
    ref: Annotated[str, typer.Argument(help="Root Preservica structural object ref to walk.")],
    collection: Annotated[str | None, typer.Option(
        help="Collection name. Defaults to a slugified version of the root entity's own "
        "title, fetched from Preservica (mirrors preservica-etl's behavior).",
    )] = None,
    group: Annotated[str, typer.Option(
        help="Bronze group namespace. Defaults to 'preservica' since this command is "
        "inherently Preservica-sourced.",
    )] = "preservica",
    max_depth: Annotated[int | None, typer.Option(
        "--max-depth", help="Maximum folder recursion depth. Defaults to unlimited.",
    )] = None,
    dest_root: Annotated[str | None, typer.Option(
        "--dest-root", help="Override the bronze root (e.g. a local directory for dev/"
        "testing instead of real CaskFS). Defaults to cask:/bronze/<group>/<collection>.",
    )] = None,
    skip_unchanged: Annotated[bool, typer.Option(
        "--skip-unchanged", help="Opt-in: skip re-downloading a file whose Preservica-"
        "reported fixity already matches the local copy from a prior import. Off by "
        "default, since this version always does a full re-walk otherwise (entity/"
        "identifiers/metadata/children JSON is still always re-fetched).",
    )] = False,
):
    """Walk a Preservica collection into bronze's by-ref/ layout."""
    opts = {
        "max_depth": max_depth,
        "dest_root": dest_root,
        "on_progress": utils.log,
        "skip_unchanged": skip_unchanged,
    }
    try:
        manifest = collection_model.preservica_import(ref, collection, group, opts)
    except ValueError as e:
        utils.error(str(e))
    except (PreservicaAuthError, PreservicaRequestError) as e:
        utils.error(str(e))

    # Errors/warnings were already streamed live via opts['on_progress'] as the
    # walk hit them, rather than saved up and dumped here at the end.
    utils.log(
        f"Collection: {manifest['collection']} — "
        f"{len(manifest['errors'])} error(s), {len(manifest['warnings'])} warning(s)"
    )

    utils.result({
        "status": "success",
        "ref": ref,
        "collection": manifest["collection"],
        "items": len(manifest["items"]),
        "errors": len(manifest["errors"]),
        "warnings": len(manifest["warnings"]),
    })


@app.command("sync")
def sync(
    collection: Annotated[str, typer.Option(help="Collection name — must already be registered (see `import`).")],
    group: Annotated[str, typer.Option(
        help="Bronze group namespace. Defaults to 'preservica', same as `import`.",
    )] = "preservica",
    changed: Annotated[list[str], typer.Option(
        "--changed", help="One changed entity, as 'TYPE:REF' (TYPE is 'SO' for a folder "
        "or 'IO' for an item), e.g. '--changed IO:1a2b3c...' or '--changed SO:9f8e7d...'. "
        "Repeat for multiple entities.",
    )] = [],
    dest_root: Annotated[str | None, typer.Option(
        "--dest-root", help="Override the bronze root — see `import`'s.",
    )] = None,
    skip_unchanged: Annotated[bool, typer.Option(
        "--skip-unchanged", help="Opt-in fixity skip for unchanged files — see `import`'s.",
    )] = False,
):
    """Sync only the specific Preservica entities that changed — never a full
    recursive re-walk (see `import` for that). Each --changed folder (SO) is
    refreshed and its immediate children diffed; each --changed item (IO) is
    fully re-fetched."""
    entities = []
    for raw in changed:
        entity_type, _, ref = raw.partition(":")
        if entity_type not in ("SO", "IO") or not ref:
            utils.error(f"--changed value must be 'SO:<ref>' or 'IO:<ref>', got {raw!r}")
        entities.append({"ref": ref, "type": entity_type})
    if not entities:
        utils.error("--changed must be given at least once.")

    opts = {"dest_root": dest_root, "on_progress": utils.log, "skip_unchanged": skip_unchanged}
    try:
        result = collection_model.preservica_sync_entities(collection, group, entities, opts)
    except ValueError as e:
        utils.error(str(e))
    except (PreservicaAuthError, PreservicaRequestError) as e:
        utils.error(str(e))

    utils.log(
        f"Synced {group}/{collection}: +{len(result['added'])} added, "
        f"-{len(result['removed'])} removed, ~{len(result['changed'])} changed "
        f"({len(result['errors'])} error(s), {len(result['warnings'])} warning(s))"
    )
    utils.result({
        "status": "success",
        "collection": collection,
        "group": group,
        "added": result["added"],
        "removed": result["removed"],
        "changed": result["changed"],
    })


@app.command("tree")
def tree(
    collection: Annotated[str, typer.Option(help="Collection name.")],
    group: Annotated[str, typer.Option(
        help="Bronze group namespace. Defaults to 'preservica', same as `import`.",
    )] = "preservica",
    dest_root: Annotated[str | None, typer.Option(
        "--dest-root", help="Override the bronze root — must match whatever --dest-root "
        "(or its cask:/ default) the matching `import` run used.",
    )] = None,
):
    """Rebuild bronze's tree/ layout from an already-imported by-ref/. Makes no
    network calls to Preservica — reads only what's already in bronze."""
    opts = {"dest_root": dest_root}
    try:
        count = collection_model.preservica_tree(collection, group, opts)
    except FileNotFoundError as e:
        utils.error(str(e))

    utils.result({"status": "success", "collection": collection, "items": count})


@app.command("changes")
def changes(
    since: Annotated[str, typer.Option(
        help="ISO-8601 timestamp/date — only changes at or after this point are reported.",
    )],
):
    """List registered Preservica collections with any change since `since` —
    the basis for Dagster's preservica_auto_sync_sensor. Also auto-registers a
    brand-new folder the first time its own 'argonath' identifier is seen."""
    try:
        result = collection_model.find_changed_preservica_collections(since)
    except ValueError as e:
        utils.error(str(e))
    except (PreservicaAuthError, PreservicaRequestError) as e:
        utils.error(str(e))

    for c in result["collections"]:
        utils.log(f"{c['group']}/{c['name']}  (ref {c['root_ref']})")
    if not result["collections"]:
        utils.log(f"(no changes since {since})")

    utils.result({"status": "success", "since": since, "collections": result["collections"]})
