import json

from typer.testing import CliRunner

from digtk.lib import items as items_lib
from digtk.main import app

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"

ITEM = items_lib.ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf", "cask:/bronze/g/c/latest/item/page.tif"],
)


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_formats_reports_every_format_present(monkeypatch):
    monkeypatch.setattr(items_lib, "resolve", lambda ark: ITEM)

    result = runner.invoke(app, ["showcase", "formats", ARK])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"ark": ARK, "formats": ["image", "pdf"]}


def test_formats_reports_empty_list_when_nothing_recognized(monkeypatch):
    empty_item = items_lib.ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.txt"],
    )
    monkeypatch.setattr(items_lib, "resolve", lambda ark: empty_item)

    result = runner.invoke(app, ["showcase", "formats", ARK])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"ark": ARK, "formats": []}


def test_formats_routes_exception_through_utils_error(monkeypatch):
    def raises(ark):
        raise LookupError("no such item")
    monkeypatch.setattr(items_lib, "resolve", raises)

    result = runner.invoke(app, ["showcase", "formats", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no such item"}


def test_formats_files_lists_each_formats_binaries(monkeypatch):
    monkeypatch.setattr(items_lib, "resolve", lambda ark: ITEM)

    result = runner.invoke(app, ["showcase", "formats", ARK, "--files"])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {
        "ark": ARK, "formats": ["image", "pdf"],
        "files": {
            "image": ["cask:/bronze/g/c/latest/item/page.tif"],
            "pdf": ["cask:/bronze/g/c/latest/item/document.pdf"],
        },
    }


def test_finalize_calls_the_finalize_model(monkeypatch):
    from digtk.models.digital_website import finalize as finalize_model
    seen = {}
    monkeypatch.setattr(finalize_model, "finalize", lambda ark, opts={}: seen.update(ark=ark) or {"status": "success", "ark": ark})

    result = runner.invoke(app, ["showcase", "finalize", ARK])

    assert result.exit_code == 0
    assert seen == {"ark": ARK}
    assert last_json_line(result.output) == {"status": "success", "ark": ARK}


def test_finalize_routes_missing_derivatives_through_utils_error(monkeypatch):
    from digtk.models.digital_website import finalize as finalize_model

    def raises(ark, opts={}):
        raise FileNotFoundError("no derivatives published yet for 1 image(s)")
    monkeypatch.setattr(finalize_model, "finalize", raises)

    result = runner.invoke(app, ["showcase", "finalize", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output)["status"] == "error"
