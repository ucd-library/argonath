import json

from typer.testing import CliRunner

from digtk.main import app
from digtk.models import collection_state

runner = CliRunner()

KEY = "bronze/preservica/haeger"
ITEM = f"{KEY}/latest/tree/a"


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_enqueue_collection_passes_the_batch(monkeypatch):
    seen = {}

    def fake(key, opts={}):
        seen.update(key=key, opts=opts)
        return {"collection": key, "items": 1, "added": 1, "enqueued": 1}
    monkeypatch.setattr(collection_state, "enqueue_collection", fake)

    result = runner.invoke(app, ["state", "enqueue-collection", "--collection-key", KEY, "--batch", "run-1"])

    assert result.exit_code == 0
    assert seen == {"key": KEY, "opts": {"batch": "run-1"}}
    assert last_json_line(result.output)["enqueued"] == 1


def test_pending_and_ack(monkeypatch):
    monkeypatch.setattr(collection_state, "pending", lambda job, limit, opts={}: [{"id": 1, "limit": limit}])
    monkeypatch.setattr(collection_state, "ack", lambda ids, opts={}: len(ids))

    pending = runner.invoke(app, ["state", "pending", "--job-name", "process_item_job", "--limit", "5"])
    ack = runner.invoke(app, ["state", "ack", "--id", "1", "--id", "2"])

    assert last_json_line(pending.output)["rows"] == [{"id": 1, "limit": 5}]
    assert last_json_line(ack.output)["removed"] == 2


def test_result_parses_the_json_and_targets_the_item(monkeypatch):
    seen = {}
    monkeypatch.setattr(collection_state, "record_result",
                        lambda job, opts={}: seen.update(job=job, opts=opts) or {"id": 1})

    result = runner.invoke(app, [
        "state", "result", "--job-name", "process_item_job", "--item", ITEM,
        "--ark", "ark:/1/a", "--result", '{"status": "success"}',
    ])

    assert result.exit_code == 0
    assert seen["job"] == "process_item_job"
    assert seen["opts"] == {"item": ITEM, "collection": None, "ark": "ark:/1/a", "result": {"status": "success"}}


def test_result_rejects_invalid_json():
    result = runner.invoke(app, ["state", "result", "--job-name", "j", "--item", ITEM, "--result", "{nope"])
    assert result.exit_code == 1
    assert "isn't valid JSON" in last_json_line(result.output)["error"]


def test_result_rejects_non_object_json():
    result = runner.invoke(app, ["state", "result", "--job-name", "j", "--item", ITEM, "--result", "[1]"])
    assert result.exit_code == 1


def test_rollup_reports_db_errors_through_utils_error(monkeypatch):
    def missing(key, opts={}):
        raise LookupError(f"collection not tracked in collection_state: {key}")
    monkeypatch.setattr(collection_state, "rollup", missing)

    result = runner.invoke(app, ["state", "rollup", "--collection-key", KEY])

    assert result.exit_code == 1
    assert "not tracked" in last_json_line(result.output)["error"]
