"""
digtk.lib.preservica — Preservica Entity API + Access Token API client, and
one-to-one XML->JSON translation, for pulling a collection out of Preservica.

Ported from the preservica-etl reference implementation (lib/preservica-client.js
+ lib/xml.js) — the same Entity/Access Token API endpoints and response handling,
adapted to httpx and digtk conventions. This library is deliberately domain-agnostic:
it knows how to talk to Preservica, not what argonath does with the result. See
digtk.models.collection for the business logic (bronze by-ref/tree layout) built on
top of this client, and digtk.lib.auth.resolve_preservica() for credential
resolution. build_client() is shared plumbing (credential resolution -> a configured
client) used both by that business logic and by plain read-only callers with no
bronze/collection concept of their own (e.g. `digtk preservica list`).
"""
from digtk.lib import auth
from digtk.lib.config import resolved_settings
from digtk.lib.preservica.client import (
    PreservicaAuthError,
    PreservicaClient,
    PreservicaRequestError,
)
from digtk.lib.preservica.xml import (
    NonXmlResponseError,
    as_list,
    attr_of,
    parse,
    parse_metadata_fragment,
    text_of,
)

__all__ = [
    "PreservicaClient",
    "PreservicaAuthError",
    "PreservicaRequestError",
    "NonXmlResponseError",
    "build_client",
    "settings_summary",
    "as_list",
    "attr_of",
    "parse",
    "parse_metadata_fragment",
    "text_of",
]


def settings_summary():
    """Current values of every preservica.* dotpath setting (see digtk.lib.config.SETTINGS)
    — the connection (tenant/username/password/base_url) and the request-timeout/
    concurrency/page-size/token-refresh-buffer tuning knobs alike, all adjustable via
    `digtk config set <dotpath> <value>` (password prompts for hidden input, same as
    any other secret setting). See also `digtk preservica config`, the CLI surface
    for this function.

    :returns: {dotpath: resolved value}. 'preservica.password' is reported as
        '***set***'/'(not set)', never its actual value (see
        digtk.lib.config.resolved_settings()).
    :rtype: dict
    """
    return {k: v for k, v in resolved_settings().items() if k.startswith("preservica.")}


def build_client(opts={}):
    """Build a PreservicaClient from resolved Preservica credentials
    (digtk.lib.auth.resolve_preservica()).

    :param dict opts: Passed through to PreservicaClient's own opts (e.g.
        'timeout_s', 'token_refresh_buffer_s', 'transport' — see PreservicaClient).
    :returns: Configured client.
    :rtype: PreservicaClient
    :raises ValueError: If no Preservica connection is configured — the message
        also lists every Preservica setting and its current value, so a caller
        sees the whole configurable surface (not just what's missing) in one
        place; see also settings_summary() and `digtk preservica config`.
    """
    creds = auth.resolve_preservica()
    if not (creds.get("base_url") and creds.get("username") and creds.get("password")):
        settings = ", ".join(f"{k}={v}" for k, v in sorted(settings_summary().items()))
        raise ValueError(
            "No Preservica connection configured. Set preservica.tenant (or "
            "preservica.base_url), preservica.username, and preservica.password via "
            "`digtk config set <dotpath> <value>` (or the equivalent environment "
            "variables). Run `digtk preservica config` to see current values. "
            f"Full Preservica settings: {settings}"
        )
    return PreservicaClient(creds["base_url"], creds["username"], creds["password"], opts)
