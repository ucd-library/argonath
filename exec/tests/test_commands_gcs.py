import json

from typer.testing import CliRunner

from digtk.lib import gcs_sync
from digtk.main import app

runner = CliRunner()

ARK = "ark:/87287/d7abc1234"


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_sync_calls_gcs_sync_with_parsed_bucket(monkeypatch):
    seen = {}

    def fake_sync_item(ark, opts={}):
        seen.update(ark=ark, opts=opts)
        return {"status": "success", "ark": ark, "bucket": opts["bucket"], "images": {"photo.tif": {}}}

    monkeypatch.setattr(gcs_sync, "sync_item", fake_sync_item)

    result = runner.invoke(app, ["gcs", "sync", ARK, "--bucket", "test-bucket"])

    assert result.exit_code == 0
    assert seen["ark"] == ARK
    assert seen["opts"]["bucket"] == "test-bucket"
    assert last_json_line(result.output)["bucket"] == "test-bucket"


def test_sync_defaults_bucket_to_config(monkeypatch):
    from digtk.lib.config import CONFIG
    seen = {}
    monkeypatch.setattr(gcs_sync, "sync_item", lambda ark, opts={}: seen.update(opts=opts) or {"status": "success"})

    runner.invoke(app, ["gcs", "sync", ARK])

    assert seen["opts"]["bucket"] == CONFIG["gcs"]["bucket"]


def test_sync_routes_exception_through_utils_error(monkeypatch):
    def raises(ark, opts={}):
        raise LookupError("no such item")
    monkeypatch.setattr(gcs_sync, "sync_item", raises)

    result = runner.invoke(app, ["gcs", "sync", ARK])

    assert result.exit_code == 1
    assert last_json_line(result.output) == {"status": "error", "error": "no such item"}
