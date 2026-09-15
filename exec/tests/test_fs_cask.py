import json

import httpx
import pytest

from digtk.lib.fs.cask import CaskBackend, CaskFsError

BASE_URL = "http://cask.test/api"


def backend(handler, token=None, trusted_user=None):
    return CaskBackend(
        BASE_URL, token=token, trusted_user=trusted_user, transport=httpx.MockTransport(handler)
    )


def test_read_bytes_sends_auth_header_and_returns_content():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"hello")

    b = backend(handler, token="tok123")
    assert b.read_bytes("/bronze/x.txt") == b"hello"
    assert seen["method"] == "GET"
    assert seen["url"] == f"{BASE_URL}/fs/bronze/x.txt"
    assert seen["auth"] == "Bearer tok123"


def test_read_bytes_sends_trusted_user_header_and_no_auth_header():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["trusted_user"] = request.headers.get("x-anduin-user")
        return httpx.Response(200, content=b"hello")

    b = backend(handler, trusted_user="dagster-worker")
    assert b.read_bytes("/bronze/x.txt") == b"hello"
    assert seen["auth"] is None
    assert seen["trusted_user"] == "dagster-worker"


def test_read_bytes_sends_both_auth_and_trusted_user_headers_when_both_set():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["trusted_user"] = request.headers.get("x-anduin-user")
        return httpx.Response(200, content=b"hello")

    b = backend(handler, token="tok123", trusted_user="dagster-worker")
    assert b.read_bytes("/bronze/x.txt") == b"hello"
    assert seen["auth"] == "Bearer tok123"
    assert seen["trusted_user"] == "dagster-worker"


def test_read_bytes_sends_neither_header_when_neither_configured():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("authorization")
        seen["trusted_user"] = request.headers.get("x-anduin-user")
        return httpx.Response(200, content=b"hello")

    b = backend(handler)
    assert b.read_bytes("/bronze/x.txt") == b"hello"
    assert seen["auth"] is None
    assert seen["trusted_user"] is None


def test_read_bytes_missing_raises_file_not_found():
    def handler(request):
        return httpx.Response(404, json={"error": "not found"})

    b = backend(handler)
    with pytest.raises(FileNotFoundError):
        b.read_bytes("/nope.txt")


def test_read_bytes_wrong_resource_type_raises_is_a_directory():
    def handler(request):
        return httpx.Response(409, json={
            "message": "This path corresponds to a directory, not a file.",
            "details": {"wrongResourceType": True, "requestedResourceType": "file"},
        })

    b = backend(handler)
    with pytest.raises(IsADirectoryError):
        b.read_bytes("/bronze/adir")


def test_read_stream_yields_chunks():
    def handler(request):
        return httpx.Response(200, content=b"streamed-content")

    b = backend(handler)
    with b.read_stream("/bronze/big.bin") as chunks:
        data = b"".join(chunks)
    assert data == b"streamed-content"


def test_write_bytes_create_posts_and_returns_file_record():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.read()
        return httpx.Response(201, json={
            "file": {"filepath": "/bronze/x.jpg", "size": 3},
            "stagedFile": {"hash_value": "abc"},
        })

    b = backend(handler)
    result = b.write_bytes("/bronze/x.jpg", b"abc", opts={"mime_type": "image/jpeg"})
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE_URL}/fs/bronze/x.jpg"
    assert seen["content_type"] == "image/jpeg"
    assert seen["body"] == b"abc"
    assert result == {"filepath": "/bronze/x.jpg", "size": 3}


def test_write_bytes_replace_puts():
    def handler(request):
        assert request.method == "PUT"
        return httpx.Response(200, json={"file": {"filepath": "/bronze/x.jpg"}})

    b = backend(handler)
    b.write_bytes("/bronze/x.jpg", b"abc", opts={"replace": True})


def test_write_bytes_sends_partition_keys_bucket_and_metadata():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(201, json={"file": {}})

    b = backend(handler)
    b.write_bytes("/bronze/x.jpg", b"abc", opts={
        "partition_keys": ["a", "b"],
        "bucket": "cold",
        "metadata": {"k": "v"},
    })
    assert seen["params"]["partition-keys"] == "a,b"
    assert seen["params"]["bucket"] == "cold"
    assert json.loads(seen["params"]["metadata"]) == {"k": "v"}


def test_write_bytes_conflict_raises_file_exists():
    def handler(request):
        return httpx.Response(409, json={"filePath": "/bronze/x.jpg", "error": "duplicate"})

    b = backend(handler)
    with pytest.raises(FileExistsError):
        b.write_bytes("/bronze/x.jpg", b"abc")


def test_write_bytes_empty_success_body_returns_empty_dict():
    """A 204/200 with no body is a valid no-content success, not an error —
    regression test for json.JSONDecodeError leaking out of a successful write."""
    def handler(request):
        return httpx.Response(201, text="")

    b = backend(handler)
    assert b.write_bytes("/bronze/x.jpg", b"abc") == {}


def test_write_bytes_non_json_success_body_raises_clear_caskfs_error():
    def handler(request):
        return httpx.Response(200, text="<html>not json</html>")

    b = backend(handler)
    with pytest.raises(CaskFsError, match="non-JSON response"):
        b.write_bytes("/bronze/x.jpg", b"abc")


def test_write_stream_sends_chunks_as_body():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(201, json={"file": {}})

    b = backend(handler)
    b.write_stream("/bronze/x.jpg", iter([b"ab", b"cd"]))
    assert seen["body"] == b"abcd"


def test_exists_true_and_false():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"filepath": "/f"})
        return httpx.Response(404, json={"error": "not found"})

    b = backend(handler)
    assert b.exists("/f") is True
    assert b.exists("/f") is False


def test_exists_true_for_directory_path():
    """/fs is a file endpoint, so a directory path gets back a 409
    wrongResourceType — that still means the path exists, not an error."""
    def handler(request):
        return httpx.Response(409, json={
            "message": "This path corresponds to a directory, not a file.",
            "details": {"wrongResourceType": True, "requestedResourceType": "file"},
        })

    b = backend(handler)
    assert b.exists("/bronze/adir") is True


def test_ls_normalizes_files_and_directories():
    def handler(request):
        assert str(request.url).startswith(f"{BASE_URL}/dir/bronze/g/c")
        return httpx.Response(200, json={
            "files": [
                {"filename": "a.txt", "filepath": "/bronze/g/c/a.txt", "size": 10, "hash_value": "h1"},
            ],
            "directories": [
                {"name": "sub", "fullname": "/bronze/g/c/sub"},
            ],
            "totalCount": 2, "limit": 20, "offset": 0,
        })

    b = backend(handler)
    entries = b.ls("/bronze/g/c", opts={"limit": 20, "offset": 0})
    by_name = {e.name: e for e in entries}
    assert by_name["a.txt"].kind == "file"
    assert by_name["a.txt"].size == 10
    assert by_name["a.txt"].hash == "h1"
    assert by_name["a.txt"].raw["filepath"] == "/bronze/g/c/a.txt"
    assert by_name["sub"].kind == "directory"
    assert by_name["sub"].path == "/bronze/g/c/sub"


def test_ls_wrong_resource_type_raises_not_a_directory():
    def handler(request):
        return httpx.Response(409, json={
            "message": "This path corresponds to a file, not a directory.",
            "details": {"wrongResourceType": True, "requestedResourceType": "directory"},
        })

    b = backend(handler)
    with pytest.raises(NotADirectoryError):
        b.ls("/bronze/g/c/a.txt")


def test_delete_sends_query_opts():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"success": True})

    b = backend(handler)
    result = b.delete("/bronze/g/c", opts={"directory": True, "soft_delete": True})
    assert seen["method"] == "DELETE"
    assert seen["params"] == {"directory": "true", "softDelete": "true"}
    assert result == {"success": True}


def test_delete_empty_success_body_returns_empty_dict():
    def handler(request):
        return httpx.Response(204, text="")

    b = backend(handler)
    assert b.delete("/bronze/g/c") == {}


def test_permission_denied_raises_permission_error():
    def handler(request):
        return httpx.Response(403, json={"error": "forbidden", "details": {"user": "u"}})

    b = backend(handler)
    with pytest.raises(PermissionError):
        b.read_bytes("/secret")


def test_bad_request_raises_value_error():
    def handler(request):
        return httpx.Response(400, json={"error": "bad metadata", "details": {"errors": []}})

    b = backend(handler)
    with pytest.raises(ValueError):
        b.write_bytes("/x", b"y")


def test_find_by_subject_sends_default_predicate_and_unwraps_single_page():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["params"] = dict(request.url.params)
        # Confirmed against a real CaskFS instance: /cask/find is paginated,
        # same envelope shape as /cask/dir — {totalCount, results: [{filepath, ...}], offset, limit}.
        return httpx.Response(200, json={
            "totalCount": 1,
            "results": [{"filepath": "/bronze/g/c/latest/item/photo.tiff.jsonld.json"}],
            "offset": 0,
            "limit": 100,
        })

    b = backend(handler)
    result = b.find_by_subject("ark:/87287/d7abc1234")
    assert seen["url"].startswith(f"{BASE_URL}/find")
    assert seen["params"]["subject"] == "ark:/87287/d7abc1234"
    assert seen["params"]["predicate"] == "https://schema.org/ark"
    assert result == ["/bronze/g/c/latest/item/photo.tiff.jsonld.json"]


def test_find_by_subject_walks_every_page(monkeypatch):
    """A 102-page multi-part item exceeds any reasonable default page size —
    regression test for exactly this: silently returning only page 1."""
    seen_offsets = []
    all_paths = [f"/bronze/g/c/latest/item/photo-{i:03d}.tif.jsonld.json" for i in range(102)]

    def handler(request):
        offset = int(dict(request.url.params)["offset"])
        limit = int(dict(request.url.params)["limit"])
        seen_offsets.append(offset)
        page = all_paths[offset:offset + limit]
        return httpx.Response(200, json={
            "totalCount": len(all_paths),
            "results": [{"filepath": p} for p in page],
            "offset": offset,
            "limit": limit,
        })

    b = backend(handler)
    result = b.find_by_subject("ark:/87287/d7abc1234", opts={"page_size": 20})
    assert result == all_paths
    assert seen_offsets == [0, 20, 40, 60, 80, 100]


def test_find_by_subject_stops_on_empty_page():
    def handler(request):
        return httpx.Response(200, json={"totalCount": 5, "results": [], "offset": 0, "limit": 100})

    b = backend(handler)
    assert b.find_by_subject("ark:/87287/d7abc1234") == []


def test_find_by_partition_keys_sends_no_subject_or_predicate():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={
            "totalCount": 1,
            "results": [{"filepath": "/bronze/g/c/latest/tree/item-one/metadata.json"}],
            "offset": 0,
            "limit": 100,
        })

    b = backend(handler)
    result = b.find_by_partition_keys(["ref-io-1", "collection-haeger"])
    assert seen["url"].startswith(f"{BASE_URL}/find")
    assert seen["params"]["partitionKeys"] == "ref-io-1,collection-haeger"
    assert "subject" not in seen["params"]
    assert "predicate" not in seen["params"]
    assert result == ["/bronze/g/c/latest/tree/item-one/metadata.json"]


def test_find_by_partition_keys_walks_every_page(monkeypatch):
    seen_offsets = []
    all_paths = [f"/bronze/g/c/latest/tree/item-{i:03d}/metadata.json" for i in range(102)]

    def handler(request):
        offset = int(dict(request.url.params)["offset"])
        limit = int(dict(request.url.params)["limit"])
        seen_offsets.append(offset)
        page = all_paths[offset:offset + limit]
        return httpx.Response(200, json={
            "totalCount": len(all_paths),
            "results": [{"filepath": p} for p in page],
            "offset": offset,
            "limit": limit,
        })

    b = backend(handler)
    result = b.find_by_partition_keys(["collection-haeger"], opts={"page_size": 20})
    assert result == all_paths
    assert seen_offsets == [0, 20, 40, 60, 80, 100]


def test_find_by_partition_keys_stops_on_empty_page():
    def handler(request):
        return httpx.Response(200, json={"totalCount": 5, "results": [], "offset": 0, "limit": 100})

    b = backend(handler)
    assert b.find_by_partition_keys(["ref-nope"]) == []


def test_find_by_subject_honors_custom_predicate_and_partition_keys():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"totalCount": 0, "results": [], "offset": 0, "limit": 100})

    b = backend(handler)
    b.find_by_subject("ark:/87287/d7abc1234", opts={
        "predicate": "http://schema.org/identifier",
        "partition_keys": ["g", "c"],
    })
    assert seen["params"]["predicate"] == "http://schema.org/identifier"
    assert seen["params"]["partitionKeys"] == "g,c"


def test_find_by_subject_raises_on_error_status():
    def handler(request):
        return httpx.Response(400, json={"error": "predicate is required"})

    b = backend(handler)
    with pytest.raises(ValueError):
        b.find_by_subject("ark:/87287/d7abc1234")


def test_unexpected_server_error_raises_caskfs_error():
    def handler(request):
        return httpx.Response(500, text="boom")

    b = backend(handler)
    with pytest.raises(CaskFsError) as exc_info:
        b.read_bytes("/x")
    assert exc_info.value.status == 500


def test_move_posts_src_and_dest_path():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"filepath": "/tree/new-name"})

    b = backend(handler)
    result = b.move("/tree/old-name", "/tree/new-name")
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE_URL}/fs/mv"
    assert seen["body"] == {"srcPath": "/tree/old-name", "destPath": "/tree/new-name"}
    assert result == {"filepath": "/tree/new-name"}


def test_move_sends_recheck_mime_type():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={})

    b = backend(handler)
    b.move("/tree/old.jpg", "/tree/new.png", opts={"recheck_mime_type": True})
    assert seen["body"] == {
        "srcPath": "/tree/old.jpg", "destPath": "/tree/new.png", "recheckMimeType": True,
    }


def test_move_missing_source_raises_file_not_found():
    def handler(request):
        return httpx.Response(404, json={"error": "not found"})

    b = backend(handler)
    with pytest.raises(FileNotFoundError):
        b.move("/tree/nope", "/tree/new-name")


def test_move_existing_destination_raises_file_exists():
    def handler(request):
        return httpx.Response(409, json={"error": "conflict"})

    b = backend(handler)
    with pytest.raises(FileExistsError):
        b.move("/tree/old-name", "/tree/already-there")


def test_add_derivative_link_posts_from_and_source_path():
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"derivative_link_id": "abc", "from_filepath": "/silver/x.jpg"})

    b = backend(handler)
    result = b.add_derivative_link("/silver/x.jpg", "/bronze/x.tiff")
    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE_URL}/lineage/add"
    assert seen["body"] == {"fromPath": "/silver/x.jpg", "sourcePath": "/bronze/x.tiff"}
    assert result == {"derivative_link_id": "abc", "from_filepath": "/silver/x.jpg"}


def test_add_derivative_link_sends_relation_and_metadata():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={})

    b = backend(handler)
    b.add_derivative_link(
        "/silver/x.jpg", "/bronze/x.tiff",
        opts={"relation": "http://schema.org/isBasedOn", "metadata": "note"},
    )
    assert seen["body"] == {
        "fromPath": "/silver/x.jpg",
        "sourcePath": "/bronze/x.tiff",
        "relation": "http://schema.org/isBasedOn",
        "metadata": "note",
    }


def test_add_derivative_link_missing_source_raises_file_not_found():
    def handler(request):
        return httpx.Response(404, json={"error": "not found"})

    b = backend(handler)
    with pytest.raises(FileNotFoundError):
        b.add_derivative_link("/silver/x.jpg", "/bronze/nope.tiff")


def test_add_derivative_link_empty_success_body_returns_empty_dict():
    def handler(request):
        return httpx.Response(200, text="")

    b = backend(handler)
    assert b.add_derivative_link("/silver/x.jpg", "/bronze/x.tiff") == {}
