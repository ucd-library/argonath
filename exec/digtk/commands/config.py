"""
digtk config — manage persistent digtk settings.

Settings resolve with this precedence: built-in default -> home config file
(~/.digtk/config.json) -> environment variable -> explicit CLI flag. See
digtk.lib.config.SETTINGS for the full registry of settings this command can set.

Subcommands:
  set   — persist a setting to the home config file
  show  — pretty-print the currently resolved settings
"""
from typing import Annotated

import typer

from digtk.lib import config as config_lib
from digtk.lib import utils

app = typer.Typer(help="Manage persistent digtk settings.", no_args_is_help=True)


@app.command(name="set")
def set_setting(
    dotpath: Annotated[str, typer.Argument(help="Setting to set, e.g. samwise.api_url.")],
    value: Annotated[str | None, typer.Argument(
        help="Value to store. Omit to be prompted (hidden input for secrets).",
    )] = None,
):
    """Persist a setting to the home config file (~/.digtk/config.json)."""
    dotpath = config_lib.resolve_alias(dotpath)
    known = {setting["dotpath"] for setting in config_lib.SETTINGS}
    if dotpath not in known:
        utils.error(f"Unknown setting '{dotpath}'. Valid settings: {', '.join(sorted(known))}")

    secret = config_lib.is_secret(dotpath)
    if value is None:
        value = typer.prompt(f"Enter value for {dotpath}", hide_input=secret)
    elif secret:
        utils.log(
            f"Warning: passing '{dotpath}' inline may leak it into shell history. "
            f"Run `digtk config set {dotpath}` with no value to be prompted instead."
        )

    try:
        config_lib.set_setting(dotpath, value)
    except ValueError as e:
        utils.error(str(e))

    utils.log(f"Set {dotpath}.")
    utils.result({"status": "success", "dotpath": dotpath})


@app.command()
def show():
    """Pretty-print the currently resolved settings, in dotpath notation."""
    resolved = config_lib.resolved_settings()
    for dotpath in sorted(resolved):
        utils.log(f"{dotpath} = {resolved[dotpath]}")
    utils.result({"status": "success", "config": resolved})
