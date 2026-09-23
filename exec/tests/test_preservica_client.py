import httpx
import pytest

from digtk.lib import auth, preservica
from digtk.lib.preservica.client import PreservicaAuthError, PreservicaClient, PreservicaRequestError

BASE_URL = "http://preservica.test"


def client(handler, **opts):
    return PreservicaClient(BASE_URL, "user", "pass", {"transport": httpx.MockTransport(handler), **opts})


# --- build_client (digtk.lib.preservica) ---------------------------------

def test_build_client_raises_when_credentials_incomplete(monkeypatch):
    monkeypatch.setattr(auth, "resolve_preservica", lambda: {"base_url": BASE_URL})
    with pytest.raises(ValueError, match="Preservica"):
        preservica.build_client()


def test_build_client_builds_configured_client(monkeypatch):
    monkeypatch.setattr(auth, "resolve_preservica", lambda: {
        "base_url": BASE_URL, "username": "user", "password": "pass",
    })
    c = preservica.build_client()
    assert isinstance(c, PreservicaClient)
    assert c.base_url == BASE_URL
    assert c.username == "user"


def test_build_client_error_lists_settable_dotpaths(monkeypatch):
    monkeypatch.setattr(auth, "resolve_preservica", lambda: {})
    with pytest.raises(ValueError, match=r"preservica\.concurrency=\d+"):
        preservica.build_client()


# --- settings_summary (digtk.lib.preservica) ------------------------------

def test_settings_summary_only_includes_preservica_dotpaths():
    settings = preservica.settings_summary()
    assert settings.keys() == {
        "preservica.tenant",
        "preservica.username",
        "preservica.password",
        "preservica.base_url",
        "preservica.request_timeout_s",
        "preservica.concurrency",
        "preservica.page_size",
        "preservica.token_refresh_buffer_s",
        "preservica.max_retries",
        "preservica.retry_backoff_s",
    }


def test_settings_summary_reports_password_as_set_or_not_without_leaking_value(monkeypatch):
    monkeypatch.delenv("PRESERVICA_PASSWORD", raising=False)
    assert preservica.settings_summary()["preservica.password"] == "(not set)"

    monkeypatch.setenv("PRESERVICA_PASSWORD", "super-secret")
    assert preservica.settings_summary()["preservica.password"] == "***set***"


def test_settings_summary_matches_resolved_config_values():
    from digtk.lib.config import CONFIG
    assert preservica.settings_summary()["preservica.concurrency"] == CONFIG["preservica"]["concurrency"]


def login_response(token="tok1", refresh="reftok1", valid_for=15):
    return httpx.Response(200, json={
        "success": True, "token": token, "refresh-token": refresh,
        "validFor": valid_for, "user": "user",
    })


# --- auth ---------------------------------------------------------------

def test_login_omits_tenant_and_stores_token():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode()
        return login_response()

    c = client(handler)
    c.login()
    assert seen["url"] == f"{BASE_URL}/api/accesstoken/login"
    assert "tenant" not in seen["body"]
    assert "username=user" in seen["body"]
    assert c.token == "tok1"
    assert c.refresh_token_ == "reftok1"


def test_login_raises_on_failure():
    def handler(request):
        return httpx.Response(200, json={"success": False, "message": "bad creds"})

    c = client(handler)
    with pytest.raises(PreservicaAuthError, match="bad creds"):
        c.login()


def test_login_raises_clear_error_on_empty_body_instead_of_json_decode_error():
    def handler(request):
        return httpx.Response(404, text="")

    c = client(handler)
    with pytest.raises(PreservicaAuthError, match="non-JSON response"):
        c.login()


def test_login_raises_clear_error_on_html_body():
    def handler(request):
        return httpx.Response(500, text="<html><body>Internal Server Error</body></html>")

    c = client(handler)
    with pytest.raises(PreservicaAuthError, match="HTTP 500"):
        c.login()


def test_request_triggers_lazy_login():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(200, text="ok")

    c = client(handler)
    resp = c.request("GET", f"{BASE_URL}/api/entity/x")
    assert resp.text == "ok"
    assert calls == ["/api/accesstoken/login", "/api/entity/x"]


def test_request_sends_access_token_header():
    seen = {}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response(token="tok-xyz")
        seen["header"] = request.headers.get("preservica-access-token")
        return httpx.Response(200, text="ok")

    c = client(handler)
    c.request("GET", f"{BASE_URL}/api/entity/x")
    assert seen["header"] == "tok-xyz"


def test_request_retries_once_on_401():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response(token="tok1")
        if request.url.path == "/api/accesstoken/refresh":
            return login_response(token="tok2")
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401)
        return httpx.Response(200, text="ok-after-retry")

    c = client(handler)
    resp = c.request("GET", f"{BASE_URL}/api/entity/x")
    assert resp.text == "ok-after-retry"
    assert c.token == "tok2"


def test_request_raises_after_second_401():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        if request.url.path == "/api/accesstoken/refresh":
            return login_response(token="tok2")
        return httpx.Response(401)

    c = client(handler)
    with pytest.raises(PreservicaRequestError) as exc_info:
        c.request("GET", f"{BASE_URL}/api/entity/x")
    assert exc_info.value.status == 401


def test_request_raises_preservica_request_error_on_non_401_failure():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(500, text="boom")

    c = client(handler)
    with pytest.raises(PreservicaRequestError) as exc_info:
        c.request("GET", f"{BASE_URL}/api/entity/x")
    assert exc_info.value.status == 500


def test_request_retries_on_transient_gateway_error_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502)
        return httpx.Response(200, text="ok-after-retry")

    c = client(handler, retry_backoff_s=0)
    resp = c.request("GET", f"{BASE_URL}/api/entity/x")
    assert resp.text == "ok-after-retry"
    assert calls["n"] == 2


def test_request_gives_up_after_max_retries_on_persistent_gateway_error():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        calls["n"] += 1
        return httpx.Response(502)

    c = client(handler, retry_backoff_s=0, max_retries=2)
    with pytest.raises(PreservicaRequestError) as exc_info:
        c.request("GET", f"{BASE_URL}/api/entity/x")
    assert exc_info.value.status == 502
    assert calls["n"] == 3  # initial attempt + 2 retries


def test_request_does_not_retry_non_retryable_status():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        calls["n"] += 1
        return httpx.Response(404)

    c = client(handler, retry_backoff_s=0, max_retries=3)
    with pytest.raises(PreservicaRequestError) as exc_info:
        c.request("GET", f"{BASE_URL}/api/entity/x")
    assert exc_info.value.status == 404
    assert calls["n"] == 1


def test_request_retries_on_dropped_connection_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("peer closed connection without sending complete message body")
        return httpx.Response(200, text="ok-after-retry")

    c = client(handler, retry_backoff_s=0)
    resp = c.request("GET", f"{BASE_URL}/api/entity/x")
    assert resp.text == "ok-after-retry"
    assert calls["n"] == 2


def test_request_gives_up_after_max_retries_on_persistent_dropped_connection():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        calls["n"] += 1
        raise httpx.RemoteProtocolError("boom")

    c = client(handler, retry_backoff_s=0, max_retries=1)
    with pytest.raises(httpx.RemoteProtocolError):
        c.request("GET", f"{BASE_URL}/api/entity/x")
    assert calls["n"] == 2  # initial attempt + 1 retry


def test_refresh_access_token_falls_back_to_login_on_failure():
    calls = {"login": 0}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            calls["login"] += 1
            return login_response(token=f"tok{calls['login']}")
        if request.url.path == "/api/accesstoken/refresh":
            return httpx.Response(200, json={"success": False})
        return httpx.Response(200, text="ok")

    c = client(handler)
    c.login()
    c.refresh_access_token()
    assert calls["login"] == 2
    assert c.token == "tok2"


def test_refresh_access_token_falls_back_to_login_on_non_json_response():
    calls = {"login": 0}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            calls["login"] += 1
            return login_response(token=f"tok{calls['login']}")
        if request.url.path == "/api/accesstoken/refresh":
            return httpx.Response(502, text="")
        return httpx.Response(200, text="ok")

    c = client(handler)
    c.login()
    c.refresh_access_token()
    assert calls["login"] == 2
    assert c.token == "tok2"


def test_get_auth_proactively_refreshes_near_expiry(monkeypatch):
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response(token="tok1", valid_for=15)
        if request.url.path == "/api/accesstoken/refresh":
            return login_response(token="tok2", valid_for=15)
        return httpx.Response(200, text="ok")

    c = client(handler)
    c.login()

    # valid_for_s=900, refresh_buffer_s defaults to 60 -> jump the clock to 30s before expiry.
    monkeypatch.setattr("time.monotonic", lambda: c.obtained_at + c.valid_for_s - 30)
    c.request("GET", f"{BASE_URL}/api/entity/x")
    assert c.token == "tok2"


def test_access_token_triggers_lazy_login_and_returns_token():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response(token="tok1")
        return httpx.Response(200, text="ok")

    c = client(handler)
    assert c.access_token() == "tok1"
    assert c.token == "tok1"


# --- entity API -----------------------------------------------------------

def test_get_children_paginates_and_parses_child_attrs():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        start = int(dict(request.url.params)["start"])
        if start == 0:
            body = (
                "<ChildrenResponse><Paging><TotalResults>2</TotalResults></Paging>"
                '<Children><Child ref="r1" title="One" type="IO">u1</Child></Children></ChildrenResponse>'
            )
        else:
            body = (
                "<ChildrenResponse><Paging><TotalResults>2</TotalResults></Paging>"
                '<Children><Child ref="r2" title="Two" type="SO">u2</Child></Children></ChildrenResponse>'
            )
        return httpx.Response(200, text=body)

    c = client(handler)
    children = c.get_children("root-ref", {"page_size": 1})
    assert children == [
        {"ref": "r1", "title": "One", "type": "IO"},
        {"ref": "r2", "title": "Two", "type": "SO"},
    ]


def test_get_children_uses_root_endpoint_when_ref_is_none():
    seen = {}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        seen["path"] = request.url.path
        return httpx.Response(200, text="<ChildrenResponse><Paging><TotalResults>0</TotalResults></Paging></ChildrenResponse>")

    c = client(handler)
    c.get_children(None)
    assert seen["path"] == "/api/entity/root/children"


def test_get_entity_expand_structure_query_param():
    seen = {}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        seen["query"] = request.url.query.decode()
        return httpx.Response(200, text="<EntityResponse><InformationObject><Title>T</Title></InformationObject></EntityResponse>")

    c = client(handler)
    entity = c.get_entity("information-objects", "ref1", {"expand_structure": True})
    assert seen["query"] == "expand=structure"
    assert entity["InformationObject"]["Title"] == "T"


def test_list_metadata_fragments():
    c = client(lambda r: login_response())
    entity_detail = {
        "AdditionalInformation": {"Metadata": {"Fragment": [
            {"@_schema": "http://oai_dc", "#text": "http://x/frag1"},
        ]}}
    }
    assert c.list_metadata_fragments(entity_detail) == [{"schemaUri": "http://oai_dc", "url": "http://x/frag1"}]


def test_list_metadata_fragments_empty_when_no_metadata_link():
    c = client(lambda r: login_response())
    assert c.list_metadata_fragments({}) == []


def test_get_metadata_fragment_returns_raw_xml_verbatim():
    raw_xml = (
        '<MetadataResponse><MetadataContainer schemaUri="http://oai_dc">'
        "<Content><dc><title>T</title></dc></Content></MetadataContainer></MetadataResponse>"
    )

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(200, text=raw_xml)

    c = client(handler)
    assert c.get_metadata_fragment(f"{BASE_URL}/metadata/frag1") == raw_xml


def test_get_identifiers():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(200, text=(
            "<IdentifiersResponse><Identifiers>"
            "<Identifier><Type>LocalID</Type><Value>abc</Value></Identifier>"
            "</Identifiers></IdentifiersResponse>"
        ))

    c = client(handler)
    assert c.get_identifiers("information-objects", "ref1") == [{"type": "LocalID", "value": "abc"}]


def test_get_bitstream_detail():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(200, text=(
            "<BitstreamResponse><Bitstream><Filename>a.tif</Filename><FileSize>100</FileSize>"
            "<Fixities><Fixity><FixityAlgorithmRef>SHA1</FixityAlgorithmRef><FixityValue>abc123</FixityValue></Fixity></Fixities>"
            "</Bitstream></BitstreamResponse>"
        ))

    c = client(handler)
    assert c.get_bitstream_detail(f"{BASE_URL}/bitstream/1") == {
        "filename": "a.tif",
        "fileSize": 100,
        "fixities": [{"algorithm": "SHA1", "value": "abc123"}],
    }


def test_resolve_content_object_file_picks_active_generation():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        if request.url.path.endswith("/generations"):
            return httpx.Response(200, text=(
                "<GenerationsResponse><Generations>"
                '<Generation active="false">http://preservica.test/gen/0</Generation>'
                '<Generation active="true">http://preservica.test/gen/1</Generation>'
                "</Generations></GenerationsResponse>"
            ))
        if request.url.path == "/gen/1":
            return httpx.Response(200, text=(
                '<GenerationResponse><Bitstreams><Bitstream filename="a.tif">http://preservica.test/bitstream/1</Bitstream>'
                "</Bitstreams></GenerationResponse>"
            ))
        if request.url.path == "/bitstream/1":
            return httpx.Response(200, text=(
                "<BitstreamResponse><Bitstream><Filename>a.tif</Filename><FileSize>10</FileSize>"
                "<Fixities><Fixity><FixityAlgorithmRef>SHA1</FixityAlgorithmRef><FixityValue>abc</FixityValue></Fixity></Fixities>"
                "</Bitstream></BitstreamResponse>"
            ))
        return httpx.Response(404)

    c = client(handler)
    file = c.resolve_content_object_file("co-ref")
    assert file == {
        "filename": "a.tif",
        "downloadUrl": "http://preservica.test/bitstream/1/content",
        "fileSize": 10,
        "fixity": {"algorithm": "SHA1", "value": "abc"},
    }


def test_resolve_content_object_file_returns_none_with_no_generations():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(200, text="<GenerationsResponse><Generations></Generations></GenerationsResponse>")

    c = client(handler)
    assert c.resolve_content_object_file("co-ref") is None


def test_resolve_all_content_resolves_each_representation():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        if request.url.path.endswith("/generations"):
            return httpx.Response(200, text=(
                "<GenerationsResponse><Generations>"
                '<Generation active="true">http://preservica.test/gen/1</Generation>'
                "</Generations></GenerationsResponse>"
            ))
        if request.url.path == "/gen/1":
            return httpx.Response(200, text=(
                '<GenerationResponse><Bitstreams><Bitstream filename="a.tif">http://preservica.test/bitstream/1</Bitstream>'
                "</Bitstreams></GenerationResponse>"
            ))
        if request.url.path == "/bitstream/1":
            return httpx.Response(200, text=(
                "<BitstreamResponse><Bitstream><Filename>a.tif</Filename><FileSize>10</FileSize>"
                "<Fixities></Fixities></Bitstream></BitstreamResponse>"
            ))
        return httpx.Response(404)

    entity_detail = {
        "Structure": {"XIP": {
            "Representation": {"Type": "Preservation", "ContentObjects": {"ContentObject": "co-ref-1"}},
            "ContentObject": {"Ref": "co-ref-1", "CustomType": "image"},
        }}
    }

    c = client(handler)
    assert c.resolve_all_content("io-ref", entity_detail) == [{
        "representationType": "Preservation",
        "contentObjectRef": "co-ref-1",
        "mimeGroup": "image",
        "filename": "a.tif",
        "downloadUrl": "http://preservica.test/bitstream/1/content",
        "fileSize": 10,
        "fixity": None,
    }]


def test_resolve_all_content_defaults_representation_type_to_preservation():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(404)

    entity_detail = {"Structure": {"XIP": {"Representation": {"ContentObjects": {"ContentObject": []}}}}}
    c = client(handler)
    assert c.resolve_all_content("io-ref", entity_detail) == []


def test_download_file_returns_bytes():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(200, content=b"filebytes")

    c = client(handler)
    assert c.download_file(f"{BASE_URL}/bitstream/1/content") == b"filebytes"


def test_get_thumbnail_returns_bytes_on_success():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(200, content=b"thumbbytes")

    c = client(handler)
    assert c.get_thumbnail("ref1") == b"thumbbytes"


def test_get_thumbnail_returns_none_on_failure():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(500)

    c = client(handler)
    assert c.get_thumbnail("ref1") is None


# --- get_entities_updated_since --------------------------------------------

def test_get_entities_updated_since_paginates_and_parses_entity_attrs():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        start = int(dict(request.url.params)["start"])
        if start == 0:
            body = (
                "<EntitiesResponse><Paging><TotalResults>2</TotalResults></Paging>"
                '<Entities><Entity ref="r1" type="SO">u1</Entity></Entities></EntitiesResponse>'
            )
        else:
            body = (
                "<EntitiesResponse><Paging><TotalResults>2</TotalResults></Paging>"
                '<Entities><Entity ref="r2" type="IO">u2</Entity></Entities></EntitiesResponse>'
            )
        return httpx.Response(200, text=body)

    c = client(handler)
    entities = c.get_entities_updated_since("2026-09-01T00:00:00Z", {"page_size": 1})
    assert entities == [
        {"ref": "r1", "type": "SO"},
        {"ref": "r2", "type": "IO"},
    ]


def test_get_entities_updated_since_passes_date_query_param():
    seen = {}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, text="<EntitiesResponse><Paging><TotalResults>0</TotalResults></Paging></EntitiesResponse>")

    c = client(handler)
    c.get_entities_updated_since("2026-09-01T00:00:00Z")
    assert seen["query"]["date"] == "2026-09-01T00:00:00+00:00"


def test_get_entities_updated_since_encodes_offset_timestamp():
    seen = {}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, text="<EntitiesResponse><Paging><TotalResults>0</TotalResults></Paging></EntitiesResponse>")

    c = client(handler)
    c.get_entities_updated_since("2026-09-13T18:23:26.328904+00:00")
    assert seen["query"]["date"] == "2026-09-13T18:23:26.328904+00:00"


def test_get_entities_updated_since_expands_bare_date_to_midnight_utc():
    seen = {}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, text="<EntitiesResponse><Paging><TotalResults>0</TotalResults></Paging></EntitiesResponse>")

    c = client(handler)
    c.get_entities_updated_since("2026-09-13")
    assert seen["query"]["date"] == "2026-09-13T00:00:00+00:00"


def test_get_entities_updated_since_assumes_utc_when_offset_omitted():
    seen = {}

    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, text="<EntitiesResponse><Paging><TotalResults>0</TotalResults></Paging></EntitiesResponse>")

    c = client(handler)
    c.get_entities_updated_since("2026-09-13T18:23:26")
    assert seen["query"]["date"] == "2026-09-13T18:23:26+00:00"


def test_get_entities_updated_since_rejects_invalid_timestamp():
    c = client(lambda request: login_response())
    with pytest.raises(ValueError, match="Invalid ISO-8601"):
        c.get_entities_updated_since("not-a-date")


def test_get_entities_updated_since_returns_empty_list_when_nothing_changed():
    def handler(request):
        if request.url.path == "/api/accesstoken/login":
            return login_response()
        return httpx.Response(200, text="<EntitiesResponse><Paging><TotalResults>0</TotalResults></Paging></EntitiesResponse>")

    c = client(handler)
    assert c.get_entities_updated_since("2026-09-01T00:00:00Z") == []
