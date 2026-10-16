import json

import pytest
from typer.testing import CliRunner

from digtk.commands import preservica
from digtk.models import collection as collection_model

runner = CliRunner()


def last_json_line(output):
    return json.loads(output.strip().splitlines()[-1])


def test_tree_full_rebuild_reports_item_count(monkeypatch):
    monkeypatch.setattr(collection_model, "preservica_tree", lambda collection, group, opts={}: 1)

    result = runner.invoke(preservica.app, ["tree", "--collection", "my-collection"])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {"status": "success", "collection": "my-collection", "items": 1}


def test_tree_changed_patches_items(monkeypatch):
    monkeypatch.setattr(
        collection_model, "preservica_tree_partial",
        lambda collection, group, entities, opts={}: {
            "moved": [], "added": ["io-2"], "removed": [], "unresolved": [],
        },
    )

    result = runner.invoke(preservica.app, [
        "tree", "--collection", "my-collection", "--changed", "IO:io-2",
    ])

    assert result.exit_code == 0
    assert last_json_line(result.output) == {
        "status": "success", "collection": "my-collection",
        "moved": [], "added": ["io-2"], "removed": [], "unresolved": [],
    }


def test_tree_raises_without_prior_import_surfaces_error(monkeypatch):
    def raise_not_found(collection, group, opts={}):
        raise FileNotFoundError("no preservicaRootRef")
    monkeypatch.setattr(collection_model, "preservica_tree", raise_not_found)

    result = runner.invoke(preservica.app, ["tree", "--collection", "my-collection"])

    assert result.exit_code == 1
