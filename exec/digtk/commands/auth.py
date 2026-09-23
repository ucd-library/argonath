"""
digtk auth — manage the CLI's CaskFS connection.

Subcommands:
  login         — authenticate against the Anduin auth gateway and store credentials
  update-cask   — copy the current login token into a caskfs CLI environment
  whoami        — show the currently resolved CaskFS connection and user
  logout        — clear stored credentials
"""
from typing import Annotated

import typer

from digtk.lib import auth, utils

app = typer.Typer(help="Manage CaskFS authentication.", no_args_is_help=True)

CaskEnvOption = Annotated[str | None, typer.Option(
    "--cask-env", help="Also write the token into this existing caskfs CLI environment.",
)]


def _sync_cask_env(env_name, creds):
    """Write `creds` into a caskfs CLI environment, exiting with an error on failure.

    :param str env_name: Name of an existing environment in ~/.caskfs/environments.json.
    :param dict creds: Credentials dict with 'token', 'user', 'api_url' keys.
    """
    try:
        auth.update_cask_env(env_name, creds["token"], creds.get("user"), creds["api_url"])
    except auth.LoginError as e:
        utils.error(str(e))
    utils.log(f"Updated caskfs environment '{env_name}' with the login token.")


@app.command()
def login(cask_env: CaskEnvOption = None):
    """Log in via the browser and store credentials in ~/.digtk/credentials.json."""
    utils.log(f"Opening browser to log in via {auth.GATEWAY_URL} ...")
    try:
        creds = auth.login()
    except auth.LoginError as e:
        utils.error(str(e))

    utils.log(f"Logged in as {creds.get('user') or '(unknown)'}.")
    if cask_env:
        _sync_cask_env(cask_env, creds)

    utils.result({"status": "success", "user": creds.get("user")})


@app.command(name="update-cask")
def update_cask(
    env: Annotated[str, typer.Argument(help="Name of an existing caskfs CLI environment to update.")],
):
    """Copy the current digtk login token into an existing caskfs CLI environment."""
    creds = auth.resolve()
    if not creds.get("token"):
        utils.error("Not logged in. Run `digtk auth login` first.")

    _sync_cask_env(env, creds)
    utils.result({"status": "success", "env": env})


@app.command()
def whoami():
    """Show the currently resolved CaskFS connection (API URL, user, token presence)."""
    creds = auth.resolve()
    if not creds.get("api_url"):
        utils.log("Not logged in. Run `digtk auth login`, or set CASK_API_URL/CASK_API_KEY.")
        utils.result({"status": "success", "logged_in": False})

    utils.log(f"CaskFS API: {creds['api_url']}")
    utils.log(f"User: {creds.get('user') or '(unknown)'}")
    utils.log(f"Token: {'set' if creds.get('token') else 'not set'}")
    utils.result({
        "status": "success",
        "logged_in": bool(creds.get("token")),
        "api_url": creds["api_url"],
        "user": creds.get("user"),
        "token_set": bool(creds.get("token")),
    })


@app.command()
def logout():
    """Clear stored CaskFS credentials."""
    auth.clear_stored()
    utils.log("Cleared stored CaskFS credentials.")
    utils.result({"status": "success"})
