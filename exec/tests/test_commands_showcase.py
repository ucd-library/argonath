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
