"""
Local filesystem backend for the fs library.

Thin wrapper around os/pathlib. Deliberately raises the native OSError subclasses
(FileNotFoundError, FileExistsError, IsADirectoryError, NotADirectoryError) rather
than a custom exception type, so callers handle errors identically regardless of
whether a path resolved to the local filesystem or CaskFS.
"""
import os
import shutil
from contextlib import contextmanager

from digtk.lib.config import CONFIG
from digtk.lib.fs.paths import DirEntry

CHUNK_SIZE = CONFIG["fs"]["chunk_size"]


def read_bytes(path):
    """Read a local file fully into memory.

    :param str path: Absolute local path.
    :returns: File contents.
    :rtype: bytes
    """
    with open(path, "rb") as f:
        return f.read()


@contextmanager
def read_stream(path):
    """Read a local file as a stream of byte chunks.

    :param str path: Absolute local path.
    :yields: Iterator of bytes chunks.
    """
    with open(path, "rb") as f:
        def chunks():
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk
        yield chunks()


def _ensure_parent(path):
    """Create the parent directory of a local path if it doesn't already exist.

    :param str path: Absolute local path.
    """
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_bytes(path, data, opts={}):
    """Write bytes to a local file.

    :param str path: Absolute local path.
    :param bytes data: Content to write.
    :param dict opts: Accepts 'replace' (bool, default False — overwrite vs. fail if the file exists).
    :raises FileExistsError: If the file exists and opts['replace'] is not truthy.
    """
    _ensure_parent(path)
    mode = "wb" if opts.get("replace") else "xb"
    with open(path, mode) as f:
        f.write(data)


def write_stream(path, chunks, opts={}):
    """Write a stream of byte chunks to a local file.

    :param str path: Absolute local path.
    :param iterator[bytes] chunks: Content chunks to write, in order.
    :param dict opts: Accepts 'replace' (bool, default False).
    :raises FileExistsError: If the file exists and opts['replace'] is not truthy.
    """
    _ensure_parent(path)
    mode = "wb" if opts.get("replace") else "xb"
    with open(path, mode) as f:
        for chunk in chunks:
            f.write(chunk)


def exists(path):
    """Check whether a local path exists.

    :param str path: Absolute local path.
    :returns: True if the path exists.
    :rtype: bool
    """
    return os.path.exists(path)


def ls(path, opts={}):
    """List entries in a local directory.

    :param str path: Absolute local path.
    :param dict opts: Unused for the local backend (pagination/query are CaskFS-only).
    :returns: Directory entries.
    :rtype: list[DirEntry]
    :raises FileNotFoundError: If the path does not exist.
    :raises NotADirectoryError: If the path is a file, not a directory.
    """
    if not os.path.isdir(path):
        if os.path.isfile(path):
            raise NotADirectoryError(path)
        raise FileNotFoundError(path)

    entries = []
    with os.scandir(path) as it:
        for e in it:
            is_dir = e.is_dir()
            entries.append(DirEntry(
                name=e.name,
                path=os.path.join(path, e.name),
                kind="directory" if is_dir else "file",
                size=None if is_dir else e.stat().st_size,
                hash=None,
                raw=None,
            ))
    return entries


def delete(path, opts={}):
    """Delete a local file or directory.

    :param str path: Absolute local path.
    :param dict opts: Accepts 'directory' (bool) to delete a directory tree instead of a single file.
    :raises FileNotFoundError: If the path does not exist.
    :raises IsADirectoryError: If the path is a directory and opts['directory'] is not truthy.
    """
    if opts.get("directory"):
        if not os.path.isdir(path):
            if os.path.isfile(path):
                raise NotADirectoryError(path)
            raise FileNotFoundError(path)
        shutil.rmtree(path)
    else:
        # os.remove()'s errno on a directory differs by platform (EISDIR on Linux,
        # EPERM on macOS/BSD) — check explicitly so behavior is consistent everywhere.
        if os.path.isdir(path):
            raise IsADirectoryError(path)
        os.remove(path)
