"""
Path routing for the fs library.

A path is a CaskFS path if it starts with the literal prefix "cask:/"
(e.g. "cask:/bronze/g/c/latest/item/photo.tiff" -> CaskFS path "/bronze/g/c/latest/item/photo.tiff").
Anything else is a local filesystem path; relative paths are resolved against cwd.
"""
import os
from dataclasses import dataclass

CASK_PREFIX = "cask:"


@dataclass(frozen=True)
class PathRef:
    """A parsed path with its routing kind and resolved location.

    :param str kind: Either 'local' or 'cask'.
    :param str raw: The original path string as passed by the caller.
    :param str path: Resolved path — absolute local path, or CaskFS-absolute path (leading '/').
    """
    kind: str
    raw: str
    path: str

    @property
    def is_cask(self):
        """
        :returns: True if this path routes to CaskFS.
        :rtype: bool
        """
        return self.kind == "cask"

    @property
    def is_local(self):
        """
        :returns: True if this path routes to the local filesystem.
        :rtype: bool
        """
        return self.kind == "local"

    def __str__(self):
        return self.raw


@dataclass(frozen=True)
class DirEntry:
    """A normalized directory listing entry, common to local and CaskFS backends.

    :param str name: Base name of the entry.
    :param str path: Full path in the same form the caller used (cask:/-prefixed or local).
    :param str kind: Either 'file' or 'directory'.
    :param int|None size: Size in bytes, if known (files only).
    :param str|None hash: Content hash, if known (CaskFS files only).
    :param dict raw: Backend-specific raw fields (e.g. CaskFS's partition_keys, digests, metadata).
    """
    name: str
    path: str
    kind: str
    size: int = None
    hash: str = None
    raw: dict = None


def parse(raw):
    """Parse a path string into a PathRef, routing it to 'cask' or 'local'.

    :param str raw: Path as provided by a caller (CLI argument, opts value, etc.).
    :returns: Parsed path reference.
    :rtype: PathRef
    """
    if raw.startswith(CASK_PREFIX + "/"):
        return PathRef(kind="cask", raw=raw, path=raw[len(CASK_PREFIX):])
    resolved = os.path.abspath(os.path.expanduser(raw))
    return PathRef(kind="local", raw=raw, path=resolved)
