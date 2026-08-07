import re

import pytest

from digtk.models import collection


def test_resolve_time_passes_through_literal():
    assert collection.resolve_time("2026-W29") == "2026-W29"


def test_resolve_time_none_stays_none():
    assert collection.resolve_time(None) is None


def test_resolve_time_current_date_is_iso_date():
    resolved = collection.resolve_time(collection.CURRENT_DATE)
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", resolved)


def test_resolve_time_current_time_is_iso_datetime():
    resolved = collection.resolve_time(collection.CURRENT_TIME)
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", resolved)


def test_dest_path_bronze_defaults_time_to_latest():
    path = collection.dest_path("bronze", "ucd-photos", {"group": "digital-collections"})
    assert path == "/bronze/digital-collections/ucd-photos/latest"


def test_dest_path_bronze_with_literal_time():
    path = collection.dest_path(
        "bronze", "ucd-photos", {"group": "digital-collections", "time": "2026-07-22"}
    )
    assert path == "/bronze/digital-collections/ucd-photos/2026-07-22"


def test_dest_path_bronze_with_current_date_sentinel():
    path = collection.dest_path(
        "bronze", "ucd-photos", {"group": "digital-collections", "time": collection.CURRENT_DATE}
    )
    assert re.match(r"^/bronze/digital-collections/ucd-photos/\d{4}-\d{2}-\d{2}$", path)


def test_dest_path_bronze_requires_group():
    with pytest.raises(ValueError, match="group"):
        collection.dest_path("bronze", "ucd-photos", {})


def test_dest_path_workspace_uses_given_username():
    path = collection.dest_path("workspace", "my-collection", {"username": "jrmerz"})
    assert path == "/workspace/jrmerz/my-collection"


def test_dest_path_workspace_defaults_username_from_env(monkeypatch):
    monkeypatch.setenv("USER", "jrmerz")
    path = collection.dest_path("workspace", "my-collection", {})
    assert path == "/workspace/jrmerz/my-collection"


def test_dest_path_workspace_rejects_time():
    with pytest.raises(ValueError, match="time"):
        collection.dest_path("workspace", "my-collection", {"username": "jrmerz", "time": "2026-07-22"})


def test_dest_path_rejects_unknown_root():
    with pytest.raises(ValueError, match="root"):
        collection.dest_path("gold", "my-collection", {})


def test_add_raises_if_source_missing():
    with pytest.raises(FileNotFoundError):
        collection.add("/no/such/path", "bronze", "ucd-photos", {"group": "digital-collections"})


def test_add_invokes_cask_cp_with_expected_argv(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv
        seen["check"] = check

    collection.add(
        str(source), "bronze", "ucd-photos",
        {"group": "digital-collections", "env": "prod", "run": fake_run},
    )

    assert seen["argv"] == [
        "cask", "cp", str(source), "/bronze/digital-collections/ucd-photos/latest",
        "-e", "prod",
    ]
    assert seen["check"] is True


def test_add_replace_and_dry_run_flags(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv

    collection.add(
        str(source), "workspace", "my-collection",
        {"username": "jrmerz", "replace": True, "dry_run": True, "run": fake_run},
    )

    assert seen["argv"] == [
        "cask", "cp", str(source), "/workspace/jrmerz/my-collection",
        "-x", "-d",
    ]


def test_add_defaults_to_leaving_cask_cps_confirmation_prompt_in_place(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv

    collection.add(
        str(source), "workspace", "my-collection",
        {"username": "jrmerz", "run": fake_run},
    )

    assert "-y" not in seen["argv"]


def test_add_yes_true_skips_confirmation_prompt(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    seen = {}

    def fake_run(argv, check):
        seen["argv"] = argv

    collection.add(
        str(source), "workspace", "my-collection",
        {"username": "jrmerz", "yes": True, "run": fake_run},
    )

    assert "-y" in seen["argv"]
