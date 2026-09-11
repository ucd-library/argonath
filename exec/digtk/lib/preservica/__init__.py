"""
digtk.lib.preservica — Preservica Entity API + Access Token API client, and
one-to-one XML->JSON translation, for pulling a collection out of Preservica.

Ported from the preservica-etl reference implementation (lib/preservica-client.js
+ lib/xml.js) — the same Entity/Access Token API endpoints and response handling,
adapted to httpx and digtk conventions. This library is deliberately domain-agnostic:
it knows how to talk to Preservica, not what argonath does with the result. See
digtk.models.collection for the business logic (bronze by-ref/tree layout) built on
top of this client, and digtk.lib.auth.resolve_preservica() for credential
resolution.
"""
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
    "as_list",
    "attr_of",
    "parse",
    "parse_metadata_fragment",
    "text_of",
]
