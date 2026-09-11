"""
PreservicaClient — Entity API (XML, hierarchy/metadata/content resolution) and
Access Token API (auth) client.

Ported from preservica-etl's lib/preservica-client.js. Confirmed-working quirks
from that port carry over directly: login must omit the `tenant` form param — the
Preservica Starter tier's sandbox derives the tenant from the hostname instead, and
passing it produces `invalid.user incorrect.tenant` — and the dedicated Content API
was found unavailable (404/500) on that tier, so file bytes are pulled through the
Entity API's bitstream `/content` sub-resource instead, and thumbnails are treated
as best-effort (see get_thumbnail()).
"""
import threading
import time

import httpx

from digtk.lib.config import CONFIG
from digtk.lib.preservica import xml as pxml

_CONFIG = CONFIG["preservica"]


class PreservicaAuthError(RuntimeError):
    """Raised when Preservica login fails outright (bad credentials, tenant, etc.)."""


class PreservicaRequestError(RuntimeError):
    """A non-2xx response from a Preservica API request (after any 401 retry).

    :param int status: HTTP status code.
    :param str url: Request URL.
    """

    def __init__(self, status, url):
        super().__init__(f"Preservica request failed: {status} ({url})")
        self.status = status
        self.url = url


class PreservicaClient:
    """Client for Preservica's Entity API and Access Token API.

    :param str base_url: Preservica base URL (e.g. 'https://mytenant.preservica.com').
    :param str username: Preservica username.
    :param str password: Preservica password.
    :param dict opts: 'timeout_s' (per-request timeout, default CONFIG['preservica']
        ['request_timeout_s']), 'token_refresh_buffer_s' (default CONFIG['preservica']
        ['token_refresh_buffer_s']), 'transport' (optional httpx.BaseTransport, used by
        tests to mock the HTTP layer via httpx.MockTransport).
    """

    def __init__(self, base_url, username, password, opts={}):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_s = opts.get("timeout_s", _CONFIG["request_timeout_s"])
        self.refresh_buffer_s = opts.get("token_refresh_buffer_s", _CONFIG["token_refresh_buffer_s"])
        self._client = httpx.Client(timeout=self.timeout_s, transport=opts.get("transport"))

        self.token = None
        self.refresh_token_ = None
        self.obtained_at = None
        self.valid_for_s = None
        # Guards login/refresh so concurrent callers (a bounded thread-pool fan-out
        # downloading an item's representations in parallel) never trigger duplicate
        # logins or consume the single-use refresh token twice.
        self._auth_lock = threading.Lock()

    def login(self):
        """Log in to the Access Token API.

        The `tenant` form param is deliberately omitted — see module docstring.

        :raises PreservicaAuthError: If the login response reports failure.
        """
        resp = self._client.post(
            f"{self.base_url}/api/accesstoken/login",
            data={"username": self.username, "password": self.password},
        )
        body = resp.json()
        if not body.get("success"):
            raise PreservicaAuthError(f"Preservica login failed: {body.get('message', 'unknown error')}")
        self._store_token(body)

    def refresh_access_token(self):
        """Exchange the current (single-use) refresh token for a new access token.

        Falls back to a full login() if the refresh itself reports failure (e.g.
        the refresh token expired).
        """
        resp = self._client.post(
            f"{self.base_url}/api/accesstoken/refresh",
            data={"refresh-token": self.refresh_token_},
            headers={"Preservica-Access-Token": self.token},
        )
        body = resp.json()
        if not body.get("success"):
            self.login()
            return
        self._store_token(body)

    def _store_token(self, body):
        """
        :param dict body: Access Token API response body.
        """
        self.token = body["token"]
        self.refresh_token_ = body.get("refresh-token")
        self.valid_for_s = (body.get("validFor") or 15) * 60
        self.obtained_at = time.monotonic()

    def _get_auth(self):
        """Return a valid access token, logging in or proactively refreshing if
        the current token is close to expiry. Guarded by a lock so concurrent
        callers never trigger duplicate logins or consume the single-use refresh
        token twice.

        :returns: A valid access token.
        :rtype: str
        """
        with self._auth_lock:
            if not self.token:
                self.login()
                return self.token
            expires_at = self.obtained_at + self.valid_for_s
            if time.monotonic() >= expires_at - self.refresh_buffer_s:
                self.refresh_access_token()
            return self.token

    def request(self, method, url, **kwargs):
        """Make an authenticated request against the Entity API, retrying once on
        a 401 (in case the proactive refresh missed a clock-skew edge case).

        :param str method: HTTP method.
        :param str url: Full URL to request.
        :param kwargs: Extra httpx.Client.request() kwargs (e.g. 'params').
        :returns: The response.
        :rtype: httpx.Response
        :raises PreservicaRequestError: If the response is a non-2xx status
            (after the single 401 retry).
        """
        return self._request(method, url, kwargs, is_retry=False)

    def _request(self, method, url, kwargs, is_retry):
        """
        :param str method: HTTP method.
        :param str url: Full URL to request.
        :param dict kwargs: Extra httpx.Client.request() kwargs, not mutated.
        :param bool is_retry: Internal flag to prevent infinite retry loops.
        :returns: The response.
        :rtype: httpx.Response
        """
        token = self._get_auth()
        headers = dict(kwargs.get("headers") or {})
        headers["Preservica-Access-Token"] = token
        resp = self._client.request(method, url, **{**kwargs, "headers": headers})

        if resp.status_code == 401 and not is_retry:
            with self._auth_lock:
                self.refresh_access_token()
            return self._request(method, url, kwargs, is_retry=True)

        if resp.status_code >= 400:
            raise PreservicaRequestError(resp.status_code, url)
        return resp

    def _get_xml(self, url):
        """Fetch and parse an XML entity endpoint.

        :param str url: Full URL to fetch.
        :returns: Parsed XML.
        :rtype: dict
        """
        resp = self.request("GET", url)
        return pxml.parse(resp.text)

    def get_children(self, ref, opts={}):
        """List paged children of a folder (or the collection root).

        :param str|None ref: Structural object ref, or None for the root.
        :param dict opts: 'page_size' (default CONFIG['preservica']['page_size']).
        :returns: Every child across all pages: [{'ref', 'title', 'type'}, ...]
            ('type' is 'SO' or 'IO').
        :rtype: list[dict]
        """
        page_size = opts.get("page_size", _CONFIG["page_size"])
        base_path = (
            f"{self.base_url}/api/entity/structural-objects/{ref}/children" if ref
            else f"{self.base_url}/api/entity/root/children"
        )

        children = []
        start = 0
        total = None
        while total is None or start < total:
            parsed = self._get_xml(f"{base_path}?start={start}&max={page_size}")
            response = parsed.get("ChildrenResponse") or {}
            total = int((response.get("Paging") or {}).get("TotalResults") or 0)

            page = pxml.as_list((response.get("Children") or {}).get("Child"))
            for child in page:
                children.append({
                    "ref": pxml.attr_of(child, "ref"),
                    "title": pxml.attr_of(child, "title"),
                    "type": pxml.attr_of(child, "type"),
                })

            start += page_size
            if not page:
                break
        return children

    def get_entity(self, data_type, ref, opts={}):
        """Get entity detail for a structural or information object.

        :param str data_type: 'structural-objects', 'information-objects', or 'content-objects'.
        :param str ref: Entity ref (UUID).
        :param dict opts: 'expand_structure' (bool) — pass `expand=structure` to
            inline the representation/content-object/generation/bitstream chain
            (information-objects only).
        :returns: Parsed EntityResponse.
        :rtype: dict
        """
        query = "?expand=structure" if opts.get("expand_structure") else ""
        parsed = self._get_xml(f"{self.base_url}/api/entity/{data_type}/{ref}{query}")
        return parsed.get("EntityResponse") or {}

    def list_metadata_fragments(self, entity_detail):
        """List descriptive metadata fragment URLs attached to an entity.

        Preservica only publishes a `Metadata` link in `AdditionalInformation` when
        fragments actually exist — there is no generic `GET {path}/{ref}/metadata`
        endpoint (it 405s). Pass the entity detail already fetched via get_entity()
        rather than re-deriving a URL.

        :param dict entity_detail: EntityResponse from get_entity().
        :returns: [{'schemaUri': str|None, 'url': str}, ...]
        :rtype: list[dict]
        """
        additional = entity_detail.get("AdditionalInformation") or {}
        fragments = pxml.as_list((additional.get("Metadata") or {}).get("Fragment"))
        return [{"schemaUri": pxml.attr_of(f, "schema"), "url": pxml.text_of(f)} for f in fragments]

    def get_metadata_fragment(self, fragment_url):
        """Fetch one descriptive metadata fragment's raw XML and one-to-one parsed form.

        :param str fragment_url: Fragment URL from list_metadata_fragments().
        :returns: {'schemaUri': str|None, 'rawXml': str, 'parsed': dict}
        :rtype: dict
        """
        resp = self.request("GET", fragment_url)
        return pxml.parse_metadata_fragment(resp.text)

    def get_identifiers(self, data_type, ref):
        """List external (third-party) identifiers attached to an entity.

        :param str data_type: 'structural-objects' or 'information-objects'.
        :param str ref: Entity ref (UUID).
        :returns: [{'type': str|None, 'value': str|None}, ...]
        :rtype: list[dict]
        """
        parsed = self._get_xml(f"{self.base_url}/api/entity/{data_type}/{ref}/identifiers")
        response = parsed.get("IdentifiersResponse") or {}
        identifiers = pxml.as_list((response.get("Identifiers") or {}).get("Identifier"))
        return [{"type": i.get("Type"), "value": i.get("Value")} for i in identifiers]

    def get_bitstream_detail(self, bitstream_url):
        """Fetch a bitstream's detail record — filename, size, fixity checksums —
        without downloading its content. Preservica already computes and stores
        these checksums, so this is a cheap way to check whether a locally held
        file is still current before re-downloading it.

        :param str bitstream_url: A bitstream's own URL (not its `/content` sub-resource).
        :returns: {'filename': str|None, 'fileSize': int|None,
            'fixities': [{'algorithm': str|None, 'value': str|None}, ...]}
        :rtype: dict
        """
        parsed = self._get_xml(bitstream_url)
        bitstream = (parsed.get("BitstreamResponse") or {}).get("Bitstream") or {}
        fixities = pxml.as_list((bitstream.get("Fixities") or {}).get("Fixity"))
        return {
            "filename": bitstream.get("Filename"),
            "fileSize": int(bitstream["FileSize"]) if bitstream.get("FileSize") is not None else None,
            "fixities": [
                {"algorithm": f.get("FixityAlgorithmRef"), "value": f.get("FixityValue")}
                for f in fixities
            ],
        }

    def resolve_content_object_file(self, content_object_ref):
        """Resolve one content object's download chain: active generation ->
        bitstream -> content URL, plus fixity info for change detection.

        :param str content_object_ref: Content object ref.
        :returns: {'filename': str, 'downloadUrl': str, 'fileSize': int|None,
            'fixity': dict|None}, or None if unresolvable.
        :rtype: dict|None
        """
        generations_parsed = self._get_xml(
            f"{self.base_url}/api/entity/content-objects/{content_object_ref}/generations"
        )
        response = generations_parsed.get("GenerationsResponse") or {}
        generations = pxml.as_list((response.get("Generations") or {}).get("Generation"))
        active = next((g for g in generations if pxml.attr_of(g, "active") == "true"), None)
        active = active if active is not None else (generations[0] if generations else None)
        if active is None:
            return None

        generation_parsed = self._get_xml(pxml.text_of(active))
        gen_response = generation_parsed.get("GenerationResponse") or {}
        bitstreams = pxml.as_list((gen_response.get("Bitstreams") or {}).get("Bitstream"))
        if not bitstreams:
            return None
        bitstream = bitstreams[0]

        bitstream_url = pxml.text_of(bitstream)
        detail = self.get_bitstream_detail(bitstream_url)

        return {
            "filename": pxml.attr_of(bitstream, "filename"),
            "downloadUrl": f"{bitstream_url}/content",
            "fileSize": detail["fileSize"],
            "fixity": detail["fixities"][0] if detail["fixities"] else None,
        }

    def resolve_all_content(self, io_ref, entity_detail=None):
        """Resolve every content object across every representation of an
        information object — a plain single-file item just has one representation
        (Type 'Preservation', Preservica's default) with one content object; a
        PAX-ingested compound item (e.g. a scanned document's pages) has multiple
        representations and/or multiple content objects per representation.

        :param str io_ref: Information object ref.
        :param dict|None entity_detail: Already-fetched `expand=structure`
            EntityResponse (e.g. from a caller that also needs it for
            title/metadata), to avoid re-fetching. Fetched internally if omitted.
        :returns: One entry per content object: {'representationType', 'contentObjectRef',
            'mimeGroup', 'filename', 'downloadUrl', 'fileSize', 'fixity'}. Empty
            if the object has no resolvable content.
        :rtype: list[dict]
        """
        entity = entity_detail or self.get_entity("information-objects", io_ref, {"expand_structure": True})
        structure_xip = (entity.get("Structure") or {}).get("XIP") or {}
        representations = pxml.as_list(structure_xip.get("Representation"))
        content_object_details = pxml.as_list(structure_xip.get("ContentObject"))

        tasks = []
        for rep in representations:
            representation_type = (rep or {}).get("Type") or "Preservation"
            for content_object_ref in pxml.as_list((rep.get("ContentObjects") or {}).get("ContentObject")):
                tasks.append((representation_type, content_object_ref))

        resolved = []
        for representation_type, content_object_ref in tasks:
            detail = next((co for co in content_object_details if co.get("Ref") == content_object_ref), None)
            file = self.resolve_content_object_file(content_object_ref)
            if file is None:
                continue
            resolved.append({
                "representationType": representation_type,
                "contentObjectRef": content_object_ref,
                "mimeGroup": (detail or {}).get("CustomType"),
                "filename": file["filename"],
                "downloadUrl": file["downloadUrl"],
                "fileSize": file["fileSize"],
                "fixity": file["fixity"],
            })
        return resolved

    def download_file(self, download_url):
        """Download a bitstream's actual bytes.

        :param str download_url: URL returned by resolve_all_content()'s entries.
        :returns: File contents.
        :rtype: bytes
        """
        return self.request("GET", download_url).content

    def get_thumbnail(self, ref):
        """Fetch a Content API thumbnail, best-effort — see module docstring for
        why this tolerates failure rather than raising.

        :param str ref: Entity ref.
        :returns: Thumbnail bytes, or None if unavailable.
        :rtype: bytes|None
        """
        try:
            return self.request("GET", f"{self.base_url}/api/content/thumbnail", params={"id": ref}).content
        except Exception:
            return None
