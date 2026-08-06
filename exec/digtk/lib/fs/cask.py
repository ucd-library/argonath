"""
CaskFS HTTP backend for the fs library.

Talks to the CaskFS REST API (see /cask/fs and /cask/dir in the caskfs repo's
docs/fs-rest-api.md). Mirrors the request shape used by caskfs's own JS HTTP
client (src/lib/http-client.js) so behavior stays in lockstep with the server.

Full URLs are built by plain string concatenation rather than httpx's base_url
joining — a leading "/" in a request path (which every CaskFS path has) would
otherwise replace the API path prefix instead of appending to it.
"""
import json as jsonlib
import mimetypes
from contextlib import contextmanager

import httpx

from digtk.lib.config import CONFIG
from digtk.lib.fs.paths import DirEntry

CHUNK_SIZE = CONFIG["fs"]["chunk_size"]


class CaskFsError(RuntimeError):
    """An unexpected (non-mapped) error response from the CaskFS HTTP API.

    :param int status: HTTP status code.
    :param str body: Raw response body text.
    """

    def __init__(self, status, body):
        super().__init__(f"CaskFS API error {status}: {body}")
        self.status = status
        self.body = body


class CaskBackend:
    """CaskFS HTTP API backend.

    :param str api_url: CaskFS API base URL (e.g. 'https://caskfs.library.ucdavis.edu/api').
    :param str token: Bearer token for authentication, if any.
    :param httpx.BaseTransport transport: Optional custom transport (used by tests to mock
        the HTTP layer via httpx.MockTransport); defaults to httpx's normal network transport.
    """

    def __init__(self, api_url, token=None, transport=None):
        self.base_url = api_url.rstrip("/")
        self.token = token
        self._client = httpx.Client(headers=self._auth_headers(), transport=transport)

    def _auth_headers(self):
        """
        :returns: Authorization header dict, or {} if no token is configured.
        :rtype: dict
        """
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _raise_for_status(self, response):
        """Translate a non-2xx CaskFS response into the matching Python builtin exception.

        :param httpx.Response response: Response to check (body must already be readable).
        :raises FileNotFoundError: 404 — path does not exist.
        :raises FileExistsError: 409 — create conflict.
        :raises IsADirectoryError: 409 — a file was expected, path is a directory.
        :raises NotADirectoryError: 409 — a directory was expected, path is a file.
        :raises PermissionError: 403 — ACL/permission denied.
        :raises ValueError: 400 — invalid request.
        :raises CaskFsError: Any other non-2xx status.
        """
        if response.status_code < 400:
            return
        status = response.status_code
        try:
            body = response.json()
        except Exception:
            body = {}
        message = body.get("message") or body.get("error") or response.text
        details = body.get("details") or {}

        if status == 404:
            raise FileNotFoundError(message)
        if status == 409:
            if details.get("wrongResourceType"):
                if details.get("requestedResourceType") == "directory":
                    raise NotADirectoryError(message)
                raise IsADirectoryError(message)
            raise FileExistsError(message)
        if status == 403:
            raise PermissionError(message)
        if status == 400:
            raise ValueError(message)
        raise CaskFsError(status, response.text)

    def read_bytes(self, path):
        """Read a CaskFS file fully into memory.

        :param str path: CaskFS-absolute path.
        :returns: File contents.
        :rtype: bytes
        """
        res = self._client.get(f"{self.base_url}/fs{path}")
        self._raise_for_status(res)
        return res.content

    @contextmanager
    def read_stream(self, path):
        """Read a CaskFS file as a stream of byte chunks.

        :param str path: CaskFS-absolute path.
        :yields: Iterator of bytes chunks.
        """
        with self._client.stream("GET", f"{self.base_url}/fs{path}") as res:
            if res.status_code >= 400:
                res.read()
            self._raise_for_status(res)
            yield res.iter_bytes(CHUNK_SIZE)

    def _write(self, method, path, body, opts):
        """Shared POST/PUT request builder for write_bytes and write_stream.

        :param str method: 'POST' (create) or 'PUT' (create-or-replace).
        :param str path: CaskFS-absolute path.
        :param bytes|iterator[bytes] body: Content to send as the request body.
        :param dict opts: 'partition_keys', 'bucket', 'metadata', 'mime_type'.
        :returns: The canonical file record from the response ('file' key), or the raw response body.
        :rtype: dict
        """
        params = {}
        if opts.get("partition_keys"):
            params["partition-keys"] = ",".join(opts["partition_keys"])
        if opts.get("bucket"):
            params["bucket"] = opts["bucket"]
        if opts.get("metadata"):
            params["metadata"] = jsonlib.dumps(opts["metadata"])

        mime_type = opts.get("mime_type") or mimetypes.guess_type(path)[0] or "application/octet-stream"

        res = self._client.request(
            method,
            f"{self.base_url}/fs{path}",
            params=params,
            headers={"Content-Type": mime_type},
            content=body,
        )
        self._raise_for_status(res)
        result = res.json()
        return result.get("file", result)

    def write_bytes(self, path, data, opts={}):
        """Write bytes to a CaskFS file.

        :param str path: CaskFS-absolute path.
        :param bytes data: Content to write.
        :param dict opts: 'replace' (bool, default False), 'partition_keys', 'metadata', 'bucket', 'mime_type'.
        :returns: The written file's CaskFS record.
        :rtype: dict
        """
        method = "PUT" if opts.get("replace") else "POST"
        return self._write(method, path, data, opts)

    def write_stream(self, path, chunks, opts={}):
        """Write a stream of byte chunks to a CaskFS file.

        :param str path: CaskFS-absolute path.
        :param iterator[bytes] chunks: Content chunks to write, in order.
        :param dict opts: Same as write_bytes.
        :returns: The written file's CaskFS record.
        :rtype: dict
        """
        method = "PUT" if opts.get("replace") else "POST"
        return self._write(method, path, chunks, opts)

    def exists(self, path):
        """Check whether a path exists in CaskFS.

        :param str path: CaskFS-absolute path.
        :returns: True if the path exists.
        :rtype: bool
        """
        res = self._client.get(f"{self.base_url}/fs{path}", params={"metadata": "true"})
        if res.status_code == 404:
            return False
        self._raise_for_status(res)
        return True

    def ls(self, path, opts={}):
        """List entries in a CaskFS directory.

        :param str path: CaskFS-absolute path.
        :param dict opts: 'limit', 'offset', 'query' (all optional; server defaults limit to 20).
        :returns: Directory entries.
        :rtype: list[DirEntry]
        """
        params = {}
        if opts.get("limit") is not None:
            params["limit"] = opts["limit"]
        if opts.get("offset") is not None:
            params["offset"] = opts["offset"]
        if opts.get("query") is not None:
            params["query"] = opts["query"]

        res = self._client.get(f"{self.base_url}/dir{path}", params=params)
        self._raise_for_status(res)
        body = res.json()

        entries = []
        for f in body.get("files", []):
            entries.append(DirEntry(
                name=f.get("filename"),
                path=f.get("filepath"),
                kind="file",
                size=f.get("size"),
                hash=f.get("hash_value"),
                raw=f,
            ))
        for d in body.get("directories", []):
            entries.append(DirEntry(
                name=d.get("name"),
                path=d.get("fullname"),
                kind="directory",
                size=None,
                hash=None,
                raw=d,
            ))
        return entries

    def delete(self, path, opts={}):
        """Delete a CaskFS file or directory.

        :param str path: CaskFS-absolute path.
        :param dict opts: 'directory' (bool) to delete a directory tree, 'soft_delete' (bool).
        :returns: Server response body.
        :rtype: dict
        """
        params = {}
        if opts.get("directory"):
            params["directory"] = "true"
        if opts.get("soft_delete"):
            params["softDelete"] = "true"
        res = self._client.request("DELETE", f"{self.base_url}/fs{path}", params=params)
        self._raise_for_status(res)
        return res.json()

    def add_derivative_link(self, path, source_path, opts={}):
        """Record CaskFS derivative-link lineage: `path` was derived from `source_path`.

        Wraps CaskFS's lineage API (POST /lineage/add) — structural metadata recording
        file-to-file provenance (e.g. a silver derivative produced from a bronze
        original), kept separate from the file bytes themselves. Upserts on repeat
        calls for the same (path, source_path, relation) triple.

        :param str path: CaskFS-absolute path of the derivative file.
        :param str source_path: CaskFS-absolute path of the source file it was derived from.
        :param dict opts: 'relation' (str, defaults server-side to schema:source),
            'metadata' (str, free-form text carried alongside the link).
        :returns: The created/updated derivative_link record.
        :rtype: dict
        :raises FileNotFoundError: If `path` or `source_path` does not exist in CaskFS.
        :raises PermissionError: If the caller lacks write access to `path` or read
            access to `source_path`.
        """
        body = {"fromPath": path, "sourcePath": source_path}
        if opts.get("relation"):
            body["relation"] = opts["relation"]
        if opts.get("metadata"):
            body["metadata"] = opts["metadata"]
        res = self._client.post(f"{self.base_url}/lineage/add", json=body)
        self._raise_for_status(res)
        return res.json()

    def find_by_subject(self, subject, opts={}):
        """Find files whose RDF graph contains a triple with the given subject,
        via CaskFS's Layer-3 search (see caskfs's docs/ld-rest-api.md, '/cask/find').

        `predicate` is a required parameter of the underlying API even though
        this method's callers only care about `subject` — every argonath
        sidecar's `ark` property expands to the predicate
        'https://schema.org/ark' under its '@vocab': 'https://schema.org/'
        JSON-LD context (see docs/cask-conventions.md), so that's used as the
        default; digtk.lib.items relies on this to resolve an ARK to its
        bronze sidecar file.

        The response is paginated (confirmed against a real CaskFS instance:
        {"totalCount": int, "results": [{"filepath": str, ...}], "offset": int,
        "limit": int} — same shape as /cask/dir's, server defaults limit to 20).
        This method transparently walks every page and returns the flat list of
        matching filepaths — callers resolving "every part of this item" need
        the complete set regardless of how many pages that takes (a multi-page
        item can easily have more matches than the default page size).

        :param str subject: Subject URI to search for (e.g. an ARK).
        :param dict opts: 'predicate' (str, default 'https://schema.org/ark'),
            'object' (str), 'graph' (str), 'partition_keys' (list[str]),
            'page_size' (int, default 100 — pagination page size for the
            underlying requests, not a cap on the total results returned).
        :returns: Every matching file's CaskFS-absolute path.
        :rtype: list[str]
        """
        params = {"subject": subject, "predicate": opts.get("predicate", "https://schema.org/ark")}
        if opts.get("object") is not None:
            params["object"] = opts["object"]
        if opts.get("graph") is not None:
            params["graph"] = opts["graph"]
        if opts.get("partition_keys"):
            params["partitionKeys"] = ",".join(opts["partition_keys"])

        page_size = opts.get("page_size", 100)
        offset = 0
        filepaths = []
        while True:
            res = self._client.get(f"{self.base_url}/find", params={**params, "limit": page_size, "offset": offset})
            self._raise_for_status(res)
            body = res.json()
            results = body.get("results", [])
            if not results:
                break
            filepaths += [r["filepath"] for r in results]
            offset += len(results)
            if offset >= body.get("totalCount", offset):
                break
        return filepaths
