import os

from digtk.lib.fs import paths


def test_parse_cask_path():
    ref = paths.parse("cask:/bronze/g/c/latest/item/photo.tiff")
    assert ref.kind == "cask"
    assert ref.is_cask
    assert not ref.is_local
    assert ref.path == "/bronze/g/c/latest/item/photo.tiff"
    assert ref.raw == "cask:/bronze/g/c/latest/item/photo.tiff"


def test_parse_cask_root_path():
    ref = paths.parse("cask:/")
    assert ref.kind == "cask"
    assert ref.path == "/"


def test_parse_absolute_local_path():
    ref = paths.parse("/tmp/foo.txt")
    assert ref.kind == "local"
    assert ref.is_local
    assert ref.path == "/tmp/foo.txt"


def test_parse_relative_local_path_resolves_against_cwd():
    ref = paths.parse("foo.txt")
    assert ref.kind == "local"
    assert ref.path == os.path.join(os.getcwd(), "foo.txt")


def test_parse_relative_local_path_with_dotdot_is_normalized():
    ref = paths.parse("../foo.txt")
    assert ref.kind == "local"
    assert ref.path == os.path.abspath(os.path.join(os.getcwd(), "../foo.txt"))
    assert ".." not in ref.path.split(os.sep)


def test_parse_expands_user():
    ref = paths.parse("~/foo.txt")
    assert ref.kind == "local"
    assert ref.path == os.path.expanduser("~/foo.txt")


def test_str_returns_raw():
    ref = paths.parse("cask:/a/b")
    assert str(ref) == "cask:/a/b"


def test_a_bare_string_starting_with_cask_but_no_slash_is_local():
    # "caskfoo" or "cask:foo" (no leading slash after the colon) must not be
    # mistaken for the cask:/ prefix.
    ref = paths.parse("cask:foo")
    assert ref.kind == "local"
