import json

from typer.testing import CliRunner

from digtk.lib import items as items_lib
from digtk.main import app

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"
ITEM_PATH = "bronze/g/c/latest/item"

ITEM = items_lib.ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf", "cask:/bronze/g/c/latest/item/page.tif"],
)

NO_ARK_ITEM = items_lib.ItemLocation(
    ark=None, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf", "cask:/bronze/g/c/latest/item/page.tif"],
)


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_formats_reports_every_format_present(monkeypatch):
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: ITEM)

    result = runner.invoke(app, ["showcase", "formats", ARK])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"item": "/bronze/g/c/latest/item", "formats": ["image", "pdf"]}


def test_formats_reports_empty_list_when_nothing_recognized(monkeypatch):
    empty_item = items_lib.ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.txt"],
    )
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: empty_item)

    result = runner.invoke(app, ["showcase", "formats", ARK])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"item": "/bronze/g/c/latest/item", "formats": []}


def test_formats_routes_exception_through_utils_error(monkeypatch):
    def raises(target, opts={}):
        raise LookupError("no such item")
    monkeypatch.setattr(items_lib, "resolve_target", raises)

    result = runner.invoke(app, ["showcase", "formats", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no such item"}


def test_formats_files_lists_each_formats_binaries(monkeypatch):
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: ITEM)

    result = runner.invoke(app, ["showcase", "formats", ARK, "--files"])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {
        "item": "/bronze/g/c/latest/item", "formats": ["image", "pdf"],
        "files": {
            "image": ["cask:/bronze/g/c/latest/item/page.tif"],
            "pdf": ["cask:/bronze/g/c/latest/item/document.pdf"],
        },
        "dir": "cask:/bronze/g/c/latest/item",
    }


def test_formats_accepts_a_bronze_path_target(monkeypatch):
    seen = {}
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: seen.update(target=target) or ITEM)

    result = runner.invoke(app, ["showcase", "formats", ITEM_PATH, "--files"])

    assert result.exit_code == 0
    assert seen["target"] == ITEM_PATH
    out = last_json_line(result.output)
    assert out["item"] == "/bronze/g/c/latest/item"
    assert out["dir"] == "cask:/bronze/g/c/latest/item"


def test_formats_succeeds_and_reports_the_path_when_item_has_no_ark(monkeypatch):
    # The core fix: resolving a bronze-path item with no ARK is not an error —
    # format detection works the same either way.
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: NO_ARK_ITEM)

    result = runner.invoke(app, ["showcase", "formats", ITEM_PATH])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"item": "/bronze/g/c/latest/item", "formats": ["image", "pdf"]}


def test_formats_requires_a_target():
    result = runner.invoke(app, ["showcase", "formats"])

    assert result.exit_code != 0


def test_finalize_calls_the_finalize_model(monkeypatch):
    from digtk.models.digital_website import finalize as finalize_model
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: ITEM)
    seen = {}
    monkeypatch.setattr(
        finalize_model, "finalize",
        lambda item, opts={}: seen.update(item=item) or {"status": "success", "ark": item.ark},
    )

    result = runner.invoke(app, ["showcase", "finalize", ARK])

    assert result.exit_code == 0
    assert seen["item"] is ITEM
    assert last_json_line(result.output) == {"status": "success", "ark": ARK}


def test_finalize_routes_missing_derivatives_through_utils_error(monkeypatch):
    from digtk.models.digital_website import finalize as finalize_model
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: ITEM)

    def raises(item, opts={}):
        raise FileNotFoundError("no derivatives published yet for 1 image(s)")
    monkeypatch.setattr(finalize_model, "finalize", raises)

    result = runner.invoke(app, ["showcase", "finalize", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output)["status"] == "error"


def test_build_item_calls_the_item_record_model(monkeypatch):
    from digtk.models.digital_website import item_record
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: ITEM)
    seen = {}
    monkeypatch.setattr(
        item_record, "build",
        lambda item, opts={}: seen.update(item=item) or {"status": "success", "ark": item.ark},
    )

    result = runner.invoke(app, ["showcase", "build-item", ARK])

    assert result.exit_code == 0
    assert seen["item"] is ITEM
    assert last_json_line(result.output) == {"status": "success", "ark": ARK}


def test_build_item_routes_no_ark_through_utils_error(monkeypatch):
    from digtk.models.digital_website import item_record
    monkeypatch.setattr(items_lib, "resolve_target", lambda target, opts={}: NO_ARK_ITEM)

    def raises(item, opts={}):
        raise LookupError(f"{ITEM_PATH} has no ark — cannot build its item record")
    monkeypatch.setattr(item_record, "build", raises)

    result = runner.invoke(app, ["showcase", "build-item", ITEM_PATH])

    assert result.exit_code == 1
    assert last_json_line(result.output)["status"] == "error"
