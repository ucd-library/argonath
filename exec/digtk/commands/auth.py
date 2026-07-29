"""
digtk auth — manage the CLI's CaskFS connection.

Subcommands:
  login   — authenticate against CaskFS and store credentials (not yet implemented)
  whoami  — show the currently resolved CaskFS connection and user
  logout  — clear stored credentials
"""
import typer

from digtk.lib import auth, utils

app = typer.Typer(help="Manage CaskFS authentication.", no_args_is_help=True)


@app.command()
def login():
    """Authenticate against CaskFS and store credentials in ~/.digtk/credentials.json."""
    raise NotImplementedError("OIDC/Keycloak login integration pending")


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
