"""watched_roots() only watches collections carrying this deployment's
showcase publish tag (see digtk.models.collection.is_published())."""
from digtk.models import cask_changes
from digtk.models import collection as collection_model


def _rows():
    return [
        {"name": "tagged", "group_name": "preservica", "workflow_tags": ["public-showcase-dev", "sync"]},
        {"name": "untagged", "group_name": "preservica", "workflow_tags": ["sync"]},
    ]


def _patch(monkeypatch):
    monkeypatch.setattr(collection_model, "list_active_collections", lambda opts={}: _rows())
    monkeypatch.setattr(
        collection_model, "items_root",
        lambda name, group, opts={}: f"cask:/bronze/{group}/{name}/latest/tree",
    )


def test_watched_roots_without_publish_tag_watches_everything(monkeypatch):
    _patch(monkeypatch)
    roots = cask_changes.watched_roots({"publish_tag": ""})
    assert [r["name"] for r in roots] == ["tagged", "untagged"]


def test_watched_roots_with_publish_tag_skips_untagged(monkeypatch):
    _patch(monkeypatch)
    roots = cask_changes.watched_roots({"publish_tag": "public-showcase-dev"})
    assert [r["name"] for r in roots] == ["tagged"]
    assert roots[0]["items_root"] == "/bronze/preservica/tagged/latest/tree"
