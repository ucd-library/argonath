"""
CaskFS adapter — unified interface for local filesystem (testing) and CaskFS HTTP API (production).

Pass --local-root to work against a local directory tree with the same layout as CaskFS.
Pass --cask-root to work against the CaskFS HTTP API.
All dc commands use this adapter so storage is swappable without test mocking.
"""
from pathlib import Path


def get_adapter(opts={}):
    """Return the appropriate CaskFS adapter based on CLI options.

    :param dict opts: Options dict with optional keys 'local_root' and 'cask_root'.
    :returns: LocalAdapter or HttpAdapter instance.
    :raises ValueError: If neither --local-root nor --cask-root is provided.
    """
    local_root = opts.get("local_root")
    cask_root = opts.get("cask_root")

    if local_root:
        return LocalAdapter(local_root)
    if cask_root:
        return HttpAdapter(cask_root)
    raise ValueError("Provide --local-root (testing) or --cask-root (production).")


class LocalAdapter:
    """CaskFS adapter backed by a local filesystem directory tree.

    Implements the same interface as HttpAdapter for use in testing
    without requiring a running CaskFS instance.

    :param str root: Absolute path to the local CaskFS root directory.
    """

    def __init__(self, root):
        """
        :param str root: Local filesystem root path.
        """
        self.root = Path(root)

    def read(self, path):
        """Read a file from the local CaskFS tree.

        :param str path: CaskFS-relative path (e.g. 'app/arda/my-collection/metadata.jsonld.json').
        :returns: File bytes.
        :rtype: bytes
        """
        raise NotImplementedError("phase 0.2")

    def write(self, path, data):
        """Write data to a file in the local CaskFS tree.

        :param str path: CaskFS-relative path.
        :param bytes data: File content to write.
        """
        raise NotImplementedError("phase 0.2")

    def exists(self, path):
        """Check whether a path exists in the local CaskFS tree.

        :param str path: CaskFS-relative path.
        :returns: True if the path exists.
        :rtype: bool
        """
        raise NotImplementedError("phase 0.2")

    def ls(self, path):
        """List entries at a path in the local CaskFS tree.

        :param str path: CaskFS-relative path.
        :returns: List of entry names.
        :rtype: list[str]
        """
        raise NotImplementedError("phase 0.2")


class HttpAdapter:
    """CaskFS adapter backed by the CaskFS HTTP API.

    :param str base_url: CaskFS server base URL (e.g. 'http://localhost:3001').
    """

    def __init__(self, base_url):
        """
        :param str base_url: CaskFS HTTP API base URL.
        """
        self.base_url = base_url.rstrip("/")

    def read(self, path):
        """Read a file via the CaskFS HTTP API.

        :param str path: CaskFS-relative path.
        :returns: File bytes.
        :rtype: bytes
        """
        raise NotImplementedError("phase 0.2")

    def write(self, path, data):
        """Write data via the CaskFS HTTP API.

        :param str path: CaskFS-relative path.
        :param bytes data: File content.
        """
        raise NotImplementedError("phase 0.2")

    def exists(self, path):
        """Check whether a path exists via the CaskFS HTTP API.

        :param str path: CaskFS-relative path.
        :returns: True if the path exists.
        :rtype: bool
        """
        raise NotImplementedError("phase 0.2")

    def ls(self, path):
        """List entries at a path via the CaskFS HTTP API.

        :param str path: CaskFS-relative path.
        :returns: List of entry names.
        :rtype: list[str]
        """
        raise NotImplementedError("phase 0.2")
