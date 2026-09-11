"""
digtk preservica — pull a collection out of Preservica into bronze.

Subcommands:
  import — walk a Preservica collection into bronze's by-ref/ layout
           (see digtk.models.collection.preservica_import)
  tree   — rebuild bronze's tree/ layout from an already-imported by-ref/
           (see digtk.models.collection.preservica_tree)
"""
from typing import Annotated

import typer

from digtk.lib import utils
from digtk.models import collection as collection_model

app = typer.Typer(help="Pull a collection out of Preservica into bronze.", no_args_is_help=True)


@app.command("import")
def import_(
    ref: Annotated[str, typer.Argument(help="Root Preservica structural object ref to walk.")],
    collection: Annotated[str, typer.Option(help="Collection name.")],
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
):
    """Walk a Preservica collection into bronze's by-ref/ layout."""
    opts = {"max_depth": max_depth, "dest_root": dest_root}
    try:
        manifest = collection_model.preservica_import(ref, collection, group, opts)
    except ValueError as e:
        utils.error(str(e))

    for warning in manifest["warnings"]:
        utils.log(f"Warning: {warning['message']} ({warning['ref']})")
    for error in manifest["errors"]:
        utils.log(f"Error [{error['stage']}]: {error['message']} ({error['ref']})")

    utils.result({
        "status": "success",
        "ref": ref,
        "collection": collection,
        "items": len(manifest["items"]),
        "errors": len(manifest["errors"]),
        "warnings": len(manifest["warnings"]),
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
    """Rebuild bronze's tree/ layout from an already-imported by-ref/."""
    opts = {"dest_root": dest_root}
    try:
        count = collection_model.preservica_tree(collection, group, opts)
    except FileNotFoundError as e:
        utils.error(str(e))

    utils.result({"status": "success", "collection": collection, "items": count})
