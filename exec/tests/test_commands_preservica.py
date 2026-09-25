import json

import pytest
from typer.testing import CliRunner

from digtk.commands import preservica
from digtk.lib.schema import ItemMetadata
from digtk.models import collection as collection_model
from digtk.models.digital_website import pipeline

runner = CliRunner()


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_tree_full_rebuild_merges_item_metadata(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        collection_model, "preservica_tree",
        lambda collection, group, opts={}: (1, [ItemMetadata(id="ark:/x/1", ark="ark:/x/1", name="Item")]),
    )
    monkeypatch.setattr(pipeline, "merge_dc_metadata", lambda item_metadata: seen.update(item=item_metadata))

    result = runner.invoke(preservica.app, ["tree", "--collection", "my-collection"])

    assert result.exit_code == 0
    assert seen["item"].ark == "ark:/x/1"
    assert last_json_line(result.output) == {"status": "success", "collection": "my-collection", "items": 1}


def test_tree_full_rebuild_merge_failure_is_logged_not_raised(monkeypatch, capsys):
    monkeypatch.setattr(
        collection_model, "preservica_tree",
        lambda collection, group, opts={}: (1, [ItemMetadata(id="ark:/x/1", ark="ark:/x/1")]),
    )

    def raising_merge(item_metadata):
        raise LookupError("ARK not yet resolvable")

    monkeypatch.setattr(pipeline, "merge_dc_metadata", raising_merge)

    result = runner.invoke(preservica.app, ["tree", "--collection", "my-collection"])

    assert result.exit_code == 0
    assert "could not merge gold metadata for ark:/x/1" in result.output
    assert last_json_line(result.output) == {"status": "success", "collection": "my-collection", "items": 1}


def test_tree_changed_patches_and_merges_added_items(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        collection_model, "preservica_tree_partial",
        lambda collection, group, entities, opts={}: {
            "moved": [], "added": ["io-2"], "removed": [], "unresolved": [],
            "item_metadata": [ItemMetadata(id="ark:/x/2", ark="ark:/x/2", name="Item Two")],
        },
    )
    monkeypatch.setattr(pipeline, "merge_dc_metadata", lambda item_metadata: seen.update(item=item_metadata))

    result = runner.invoke(preservica.app, [
        "tree", "--collection", "my-collection", "--changed", "IO:io-2",
    ])

    assert result.exit_code == 0
    assert seen["item"].ark == "ark:/x/2"
    payload = last_json_line(result.output)
    assert payload == {
        "status": "success", "collection": "my-collection",
        "moved": [], "added": ["io-2"], "removed": [], "unresolved": [],
    }
    assert "item_metadata" not in payload


def test_tree_raises_without_prior_import_surfaces_error(monkeypatch):
    def raise_not_found(collection, group, opts={}):
        raise FileNotFoundError("no preservicaRootRef")
    monkeypatch.setattr(collection_model, "preservica_tree", raise_not_found)

    result = runner.invoke(preservica.app, ["tree", "--collection", "my-collection"])

    assert result.exit_code == 1
