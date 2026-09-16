import os

import pytest

from digtk.lib.fs import local


def test_write_bytes_and_read_bytes_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "sub" / "file.txt")
    local.write_bytes(path, b"hello")
    assert local.read_bytes(path) == b"hello"


def test_write_bytes_fails_if_exists_without_replace(tmp_path):
    path = str(tmp_path / "file.txt")
    local.write_bytes(path, b"one")
    with pytest.raises(FileExistsError):
        local.write_bytes(path, b"two")
    assert local.read_bytes(path) == b"one"


def test_write_bytes_replace_overwrites(tmp_path):
    path = str(tmp_path / "file.txt")
    local.write_bytes(path, b"one")
    local.write_bytes(path, b"two", opts={"replace": True})
    assert local.read_bytes(path) == b"two"


def test_read_bytes_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        local.read_bytes(str(tmp_path / "nope.txt"))


def test_read_bytes_on_directory_raises_is_a_directory(tmp_path):
    with pytest.raises(IsADirectoryError):
        local.read_bytes(str(tmp_path))


def test_read_stream_yields_chunks(tmp_path):
    path = str(tmp_path / "file.txt")
    local.write_bytes(path, b"hello world")
    with local.read_stream(path) as chunks:
        data = b"".join(chunks)
    assert data == b"hello world"


def test_write_stream_from_iterator(tmp_path):
    path = str(tmp_path / "file.txt")
    local.write_stream(path, iter([b"ab", b"cd"]))
    assert local.read_bytes(path) == b"abcd"


def test_exists(tmp_path):
    path = str(tmp_path / "file.txt")
    assert local.exists(path) is False
    local.write_bytes(path, b"x")
    assert local.exists(path) is True


def test_ls_lists_files_and_directories(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"hi")
    (tmp_path / "sub").mkdir()
    entries = local.ls(str(tmp_path))
    by_name = {e.name: e for e in entries}
    assert set(by_name) == {"a.txt", "sub"}
    assert by_name["a.txt"].kind == "file"
    assert by_name["a.txt"].size == 2
    assert by_name["sub"].kind == "directory"
    assert by_name["sub"].size is None


def test_ls_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        local.ls(str(tmp_path / "nope"))


def test_ls_on_file_raises_not_a_directory(tmp_path):
    path = str(tmp_path / "file.txt")
    local.write_bytes(path, b"x")
    with pytest.raises(NotADirectoryError):
        local.ls(path)


def test_move_file_to_new_path(tmp_path):
    src = str(tmp_path / "old.txt")
    dest = str(tmp_path / "sub" / "new.txt")
    local.write_bytes(src, b"hello")
    local.move(src, dest)
    assert not os.path.exists(src)
    assert local.read_bytes(dest) == b"hello"


def test_move_directory_moves_whole_subtree(tmp_path):
    src = tmp_path / "old-dir"
    (src / "nested").mkdir(parents=True)
    (src / "a.txt").write_bytes(b"a")
    (src / "nested" / "b.txt").write_bytes(b"b")
    dest = str(tmp_path / "renamed" / "new-dir")

    local.move(str(src), dest)

    assert not src.exists()
    assert local.read_bytes(os.path.join(dest, "a.txt")) == b"a"
    assert local.read_bytes(os.path.join(dest, "nested", "b.txt")) == b"b"


def test_move_missing_source_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        local.move(str(tmp_path / "nope.txt"), str(tmp_path / "dest.txt"))


def test_move_existing_destination_raises_file_exists(tmp_path):
    src = str(tmp_path / "src.txt")
    dest = str(tmp_path / "dest.txt")
    local.write_bytes(src, b"x")
    local.write_bytes(dest, b"y")
    with pytest.raises(FileExistsError):
        local.move(src, dest)


def test_delete_file(tmp_path):
    path = str(tmp_path / "file.txt")
    local.write_bytes(path, b"x")
    local.delete(path)
    assert not os.path.exists(path)


def test_delete_missing_raises_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        local.delete(str(tmp_path / "nope.txt"))


def test_delete_directory_requires_directory_opt(tmp_path):
    d = tmp_path / "sub"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        local.delete(str(d))
    local.delete(str(d), opts={"directory": True})
    assert not d.exists()
