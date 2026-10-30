from types import SimpleNamespace

import pytest

from digtk.lib import derivative, fs, items, reporting
from digtk.lib.items import ItemLocation
from digtk.models import accessibility, figures, ocr, split, workspace
from digtk.models import collection as collection_model

ARK = "ark:/87287/d7abc1234"

WS_ITEM = ItemLocation(
    ark=None, group="jdoe", collection="box-12", time=None, item_path="folder-3",
    bronze_binary_paths=[
        "cask:/workspace/jdoe/box-12/source/folder-3/photo-a.tiff",
        "cask:/workspace/jdoe/box-12/source/folder-3/document.pdf",
    ],
    dest_type="workspace", dest_prefix="derivatives",
)
IMAGES_ONLY_WS_ITEM = ItemLocation(
    ark=None, group="jdoe", collection="box-12", time=None, item_path="",
    bronze_binary_paths=["cask:/workspace/jdoe/box-12/source/cover.jpg"],
    dest_type="workspace", dest_prefix="derivatives",
)
BRONZE_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="box-12/folder-3",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/box-12/folder-3/document.pdf",
        "cask:/bronze/g/c/latest/box-12/folder-3/photo-a.tiff",
    ],
    sidecar_path="cask:/bronze/g/c/latest/box-12/folder-3/photo-a.tiff.jsonld.json",
)


def _entry(name, path, kind):
    return SimpleNamespace(name=name, path=path, kind=kind)


def _fake_ls(tree):
    """fs.ls stub over {'cask:/dir': [(name, kind), ...]}; records opts."""
    calls = []

    def fake_ls(path, opts={}):
        calls.append((path, opts))
        if path not in tree:
            raise FileNotFoundError(path)
        raw = path[len("cask:"):]
        return [_entry(name, f"{raw}/{name}", kind) for name, kind in tree[path]]
    fake_ls.calls = calls
    return fake_ls


@pytest.fixture
def tracked(monkeypatch):
    """Replace reporting.track with a recorder (no Postgres)."""
    calls = []

    class FakeTrack:
        def __init__(self, job_id, stage, item_id=None, collection=None, options=None):
            calls.append({"job_id": job_id, "stage": stage, "item_id": item_id, "collection": collection})

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(reporting, "track", FakeTrack)
    return calls


# --- stage / resolve_targets -------------------------------------------------------

def test_stage_uploads_folder_into_source_dir(tmp_path, monkeypatch):
    folder = tmp_path / "box-12"
    folder.mkdir()
    seen = {}
    monkeypatch.setattr(collection_model, "add", lambda source, root, coll, opts={}: seen.update(
        source=source, root=root, collection=coll, opts=opts,
    ))

    root = workspace.stage(str(folder), opts={"username": "jdoe"})

    assert root == "cask:/workspace/jdoe/box-12"
    assert seen["root"] == "workspace"
    assert seen["collection"] == "box-12"
    assert seen["opts"]["subdir"] == "source"
    assert seen["opts"]["username"] == "jdoe"
    assert seen["opts"]["yes"] is True


def test_stage_folder_opt_overrides_local_folder_name(tmp_path, monkeypatch):
    monkeypatch.setattr(collection_model, "add", lambda *a, **k: None)
    assert workspace.stage(str(tmp_path), opts={"username": "jdoe", "folder": "album"}) == (
        "cask:/workspace/jdoe/album"
    )


def test_stage_rejects_a_non_directory(tmp_path):
    f = tmp_path / "scan.tiff"
    f.write_bytes(b"x")
    with pytest.raises(ValueError, match="not a local directory"):
        workspace.stage(str(f))


def test_resolve_targets_ark_is_one_bronze_item(monkeypatch):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: BRONZE_ITEM)
    assert workspace.resolve_targets(ARK) == [BRONZE_ITEM]


@pytest.mark.parametrize("path", [
    "cask:/bronze/preservica/haeger/latest/tree/quarter-mile",
    "bronze/preservica/haeger/latest/tree/quarter-mile",
])
def test_resolve_targets_bronze_folder_is_one_item(monkeypatch, path):
    seen = []
    monkeypatch.setattr(items, "resolve_path", lambda p, opts={}: seen.append(p) or BRONZE_ITEM)
    monkeypatch.setattr(items, "list_workspace_items", lambda p: pytest.fail("not a workspace path"))
    assert workspace.resolve_targets(path) == [BRONZE_ITEM]
    assert seen == [path]


def test_resolve_targets_workspace_path_covers_every_item_under_it(monkeypatch):
    monkeypatch.setattr(items, "list_workspace_items", lambda path: [
        "cask:/workspace/jdoe/box-12/source", "cask:/workspace/jdoe/box-12/source/folder-3",
    ])
    monkeypatch.setattr(items, "resolve_workspace", lambda path: path)
    assert workspace.resolve_targets("cask:/workspace/jdoe/box-12") == [
        "cask:/workspace/jdoe/box-12/source", "cask:/workspace/jdoe/box-12/source/folder-3",
    ]


def test_resolve_targets_stages_a_local_folder_first(tmp_path, monkeypatch):
    staged = {}
    monkeypatch.setattr(workspace, "stage", lambda path, opts={}: staged.update(path=path, opts=opts) or "cask:/workspace/jdoe/box-12")
    monkeypatch.setattr(items, "list_workspace_items", lambda path: [f"{path}/source"])
    monkeypatch.setattr(items, "resolve_workspace", lambda path: path)

    result = workspace.resolve_targets(str(tmp_path), opts={"folder": "box-12"})

    assert staged["path"] == str(tmp_path)
    assert staged["opts"]["folder"] == "box-12"
    assert result == ["cask:/workspace/jdoe/box-12/source"]


def test_resolve_targets_rejects_a_missing_local_path():
    with pytest.raises(ValueError, match="expected an ARK"):
        workspace.resolve_targets("./does-not-exist")


def test_resolve_targets_raises_when_workspace_path_is_empty(monkeypatch):
    monkeypatch.setattr(items, "list_workspace_items", lambda path: [])
    with pytest.raises(LookupError, match="no files found"):
        workspace.resolve_targets("cask:/workspace/jdoe/box-12")


# --- apply_to_targets ---------------------------------------------------------------

def test_apply_to_targets_single_item_returns_fn_result_and_reports(monkeypatch, tracked):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: BRONZE_ITEM)

    result = workspace.apply_to_targets(ARK, lambda item, opts: {"status": "success", "seen": opts}, opts={
        "stage": "alt-text", "fn_opts": {"max_alt_chars": 100},
        "reporting_job_id": "run-1", "reporting_collection": "g/c",
    })

    assert result == {"status": "success", "seen": {"max_alt_chars": 100}}
    assert tracked == [{"job_id": "run-1", "stage": "alt-text", "item_id": "/bronze/g/c/latest/box-12/folder-3", "collection": "g/c"}]


def test_apply_to_targets_many_items_returns_items_list(monkeypatch, tracked):
    monkeypatch.setattr(workspace, "resolve_targets", lambda target, opts={}: [WS_ITEM, IMAGES_ONLY_WS_ITEM])

    result = workspace.apply_to_targets("cask:/workspace/jdoe/box-12", lambda item, opts: {"item": items.item_id(item)},
                                        opts={"stage": "ocr"})

    assert result == {"status": "success", "items": [
        {"item": "cask:/workspace/jdoe/box-12/source/folder-3"},
        {"item": "cask:/workspace/jdoe/box-12/source"},
    ]}
    assert [c["item_id"] for c in tracked] == [
        "cask:/workspace/jdoe/box-12/source/folder-3", "cask:/workspace/jdoe/box-12/source",
    ]


# --- run --------------------------------------------------------------------------

@pytest.fixture
def fake_steps(monkeypatch):
    calls = []

    def recorder(name):
        return lambda item, opts={}: calls.append((name, items.item_id(item), opts)) or {"status": "success", "step": name}

    monkeypatch.setitem(workspace.STEPS, "split-detect", (recorder("split-detect"), {"image"}))
    monkeypatch.setitem(workspace.STEPS, "split-apply", (recorder("split-apply"), {"image"}))
    monkeypatch.setitem(workspace.STEPS, "ocr", (recorder("ocr"), {"image", "pdf"}))
    monkeypatch.setitem(workspace.STEPS, "alt-text", (recorder("alt-text"), {"image", "pdf"}))
    monkeypatch.setitem(workspace.STEPS, "tag-pdf", (recorder("tag-pdf"), {"pdf"}))
    return calls


def test_run_executes_steps_in_fixed_order_and_skips_inapplicable_ones(monkeypatch, fake_steps, tracked):
    monkeypatch.setattr(workspace, "resolve_targets", lambda target, opts={}: [WS_ITEM, IMAGES_ONLY_WS_ITEM])

    result = workspace.run("cask:/workspace/jdoe/box-12", opts={
        "steps": ["tag-pdf", "ocr", "alt-text"],
        "step_opts": {"ocr": {"vision": False}},
        "reporting_job_id": "run-1",
    })

    assert [(name, item) for name, item, _ in fake_steps] == [
        ("ocr", "cask:/workspace/jdoe/box-12/source/folder-3"),
        ("alt-text", "cask:/workspace/jdoe/box-12/source/folder-3"),
        ("tag-pdf", "cask:/workspace/jdoe/box-12/source/folder-3"),
        ("ocr", "cask:/workspace/jdoe/box-12/source"),
        ("alt-text", "cask:/workspace/jdoe/box-12/source"),
    ]
    assert fake_steps[0][2] == {"vision": False}
    second = result["items"][1]
    assert second["steps"]["tag-pdf"] == {"skipped": "no pdf binaries"}
    assert [c["stage"] for c in tracked] == ["ocr", "alt-text", "tag-pdf", "ocr", "alt-text"]


@pytest.mark.parametrize("steps", [[], ["ocr", "nope"]])
def test_run_rejects_missing_or_unknown_steps(steps):
    with pytest.raises(ValueError, match="steps must be"):
        workspace.run("cask:/workspace/jdoe/box-12", opts={"steps": steps})


def test_steps_point_at_the_real_tool_functions():
    assert workspace.STEPS["split-detect"][0] is split.detect
    assert workspace.STEPS["ocr"][0] is ocr.run_ocr
    assert workspace.STEPS["alt-text"][0] is accessibility.generate_alt_text
    assert workspace.STEPS["tag-pdf"][0] is accessibility.tag_pdf
    assert workspace.STEPS["ocr-correct"][0] is ocr.correct_ocr
    assert workspace.STEPS["pdf-build"] == (accessibility.build_pdf, {"image"})
    assert workspace.STEPS["figures"] == (accessibility.detect_figures, {"image", "pdf"})


def test_figures_run_before_documents_are_built():
    order = workspace.STEP_ORDER
    assert order.index("alt-text") < order.index("figures") < order.index("pdf-build") < order.index("tag-pdf")


def test_pdf_build_is_a_folder_step_that_runs_before_tag_pdf():
    assert "pdf-build" in workspace.FOLDER_STEPS
    order = workspace.STEP_ORDER
    assert order.index("ocr") < order.index("pdf-build") < order.index("tag-pdf")


def test_ocr_correct_runs_after_ocr_and_before_tag_pdf():
    order = workspace.STEP_ORDER
    assert order.index("ocr") < order.index("ocr-correct") < order.index("tag-pdf")


# --- list_folders / pull --------------------------------------------------------

def test_list_folders_only_includes_dirs_with_a_source_dir(monkeypatch):
    monkeypatch.setattr(fs, "ls", _fake_ls({
        "cask:/workspace": [("jdoe", "directory")],
        "cask:/workspace/jdoe": [("box-12", "directory"), ("uploads", "directory"), ("notes.txt", "file")],
        "cask:/workspace/jdoe/box-12": [("source", "directory"), ("derivatives", "directory")],
        "cask:/workspace/jdoe/uploads": [("raw.zip", "file")],
    }))

    assert workspace.list_folders() == [{
        "username": "jdoe", "folder": "box-12", "key": "workspace/jdoe/box-12",
        "path": "cask:/workspace/jdoe/box-12",
    }]


def test_pull_downloads_the_tree_keeping_its_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(fs, "ls", _fake_ls({
        "cask:/workspace/jdoe/box-12/derivatives": [("split-spreads", "directory")],
        "cask:/workspace/jdoe/box-12/derivatives/split-spreads": [("gutters.json", "file"), ("review-sheets", "directory")],
        "cask:/workspace/jdoe/box-12/derivatives/split-spreads/review-sheets": [("sheet-01.jpg", "file")],
    }))
    writes = []
    monkeypatch.setattr(fs, "write", lambda path, data=None, src=None, opts={}: writes.append((path, src)))

    result = workspace.pull("cask:/workspace/jdoe/box-12/derivatives/", str(tmp_path))

    assert sorted(writes) == sorted([
        (str(tmp_path / "split-spreads/gutters.json"), "cask:/workspace/jdoe/box-12/derivatives/split-spreads/gutters.json"),
        (str(tmp_path / "split-spreads/review-sheets/sheet-01.jpg"),
         "cask:/workspace/jdoe/box-12/derivatives/split-spreads/review-sheets/sheet-01.jpg"),
    ])
    assert (tmp_path / "split-spreads/review-sheets").is_dir()
    assert len(result["files"]) == 2


# --- promote ------------------------------------------------------------------------

WS_DERIV = "cask:/workspace/jdoe/box-12/derivatives/folder-3"

PROMOTE_TREE = {
    WS_DERIV: [
        ("text", "directory"), ("accessibility", "directory"), ("document", "directory"),
        ("sub-item", "directory"),  # a nested workspace item's own outputs — never promoted
    ],
    f"{WS_DERIV}/text": [("photo-a-ocr.hocr", "file"), ("photo-a-transcript.txt", "file")],
    f"{WS_DERIV}/document": [("text", "directory")],
    f"{WS_DERIV}/document/text": [("page-000", "directory")],
    f"{WS_DERIV}/document/text/page-000": [("ocr.hocr", "file")],
    f"{WS_DERIV}/accessibility": [("document.pdf", "directory")],
    f"{WS_DERIV}/accessibility/document.pdf": [("tagged.pdf", "file")],
    f"{WS_DERIV}/sub-item": [("text", "directory")],
}


@pytest.fixture
def promote_env(monkeypatch):
    monkeypatch.setattr(items, "resolve_workspace", lambda path: WS_ITEM)
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: BRONZE_ITEM)
    monkeypatch.setattr(fs, "ls", _fake_ls(PROMOTE_TREE))
    monkeypatch.setattr(fs, "read", lambda path: f"DATA:{path}".encode())
    monkeypatch.setattr(fs, "exists", lambda path: False)
    written = []

    def fake_write(*, type, collection, rel_path, data, source_files, opts={}):
        written.append({"type": type, "collection": collection, "rel_path": rel_path,
                        "data": data, "source_files": source_files, "opts": opts})
        return {"path": f"cask:/silver/{opts['group']}/{collection}/{opts['time']}/{rel_path}", "links": []}
    monkeypatch.setattr(derivative, "write", fake_write)
    return written


def test_promote_copies_tool_outputs_into_silver_with_bronze_lineage(promote_env):
    result = workspace.promote("cask:/workspace/jdoe/box-12/source/folder-3", ARK)

    by_rel = {w["rel_path"]: w for w in promote_env}
    assert set(by_rel) == {
        "box-12/folder-3/text/photo-a-ocr.hocr",
        "box-12/folder-3/text/photo-a-transcript.txt",
        "box-12/folder-3/document/text/page-000/ocr.hocr",
        "box-12/folder-3/accessibility/document.pdf/tagged.pdf",
    }
    for write in promote_env:
        assert write["type"] == "silver"
        assert write["collection"] == "c"
        assert write["opts"]["group"] == "g" and write["opts"]["time"] == "latest"

    hocr = by_rel["box-12/folder-3/text/photo-a-ocr.hocr"]
    assert hocr["data"] == f"DATA:{WS_DERIV}/text/photo-a-ocr.hocr".encode()
    assert hocr["source_files"] == ["cask:/bronze/g/c/latest/box-12/folder-3/photo-a.tiff"]
    assert hocr["opts"]["link_relation"] == ocr.HOCR_LINK_RELATION
    assert by_rel["box-12/folder-3/text/photo-a-transcript.txt"]["opts"]["link_relation"] == ocr.TRANSCRIPT_LINK_RELATION

    tagged = by_rel["box-12/folder-3/accessibility/document.pdf/tagged.pdf"]
    assert tagged["source_files"] == ["cask:/bronze/g/c/latest/box-12/folder-3/document.pdf"]
    assert tagged["opts"]["link_relation"] == accessibility.TAGGED_PDF_LINK_RELATION
    assert by_rel["box-12/folder-3/document/text/page-000/ocr.hocr"]["source_files"] == [
        "cask:/bronze/g/c/latest/box-12/folder-3/document.pdf",
    ]

    assert result["dry_run"] is False
    assert len(result["promoted"]) == 4


def test_promote_dry_run_writes_nothing(promote_env):
    result = workspace.promote("cask:/workspace/jdoe/box-12/source/folder-3", ARK, opts={"dry_run": True})

    assert promote_env == []
    assert {p["to"] for p in result["promoted"]} >= {
        "cask:/silver/g/c/latest/box-12/folder-3/text/photo-a-ocr.hocr",
    }


def test_promote_refuses_to_overwrite_existing_silver_outputs_by_default(promote_env, monkeypatch):
    monkeypatch.setattr(fs, "exists", lambda path: path.endswith("tagged.pdf"))

    with pytest.raises(ValueError, match="already exist in silver"):
        workspace.promote("cask:/workspace/jdoe/box-12/source/folder-3", ARK)
    assert promote_env == []


def test_promote_replace_overwrites(promote_env, monkeypatch):
    monkeypatch.setattr(fs, "exists", lambda path: True)

    workspace.promote("cask:/workspace/jdoe/box-12/source/folder-3", ARK, opts={"replace": True})

    assert promote_env and all(w["opts"]["replace"] is True for w in promote_env)


def test_promote_requires_matching_bronze_filenames(promote_env, monkeypatch):
    monkeypatch.setattr(items, "resolve", lambda ark, opts={}: ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="x",
        bronze_binary_paths=["cask:/bronze/g/c/latest/x/other.tiff"],
    ))

    with pytest.raises(ValueError, match="no same-named bronze binary"):
        workspace.promote("cask:/workspace/jdoe/box-12/source/folder-3", ARK)


def test_promote_raises_when_nothing_to_promote(promote_env, monkeypatch):
    monkeypatch.setattr(fs, "ls", _fake_ls({}))

    with pytest.raises(LookupError, match="no tool outputs"):
        workspace.promote("cask:/workspace/jdoe/box-12/source/folder-3", ARK)


@pytest.mark.parametrize("rel_path, relation", [
    ("split-spreads/gutters.json", split.GUTTERS_LINK_RELATION),
    ("split-spreads/review/a.jpg", split.REVIEW_LINK_RELATION),
    ("split-spreads/review-sheets/sheet-01.jpg", split.REVIEW_LINK_RELATION),
    ("split-spreads/pages/a_L.tif", split.SPLIT_PAGE_LINK_RELATION),
    ("accessibility/alt-text.json", accessibility.ALT_TEXT_LINK_RELATION),
    ("accessibility/document.pdf/1/layout.json", accessibility.LAYOUT_LINK_RELATION),
    ("text/ocr-result.json", ocr.HOCR_LINK_RELATION),
    ("text/page-000/ocr-corrected.json", ocr.CORRECTED_LINK_RELATION),
    ("accessibility/document/searchable.pdf", accessibility.SEARCHABLE_PDF_LINK_RELATION),
    ("accessibility/document/text.txt", accessibility.DOCUMENT_TEXT_LINK_RELATION),
    ("accessibility/report.pdf/2/figures.json", figures.FIGURES_LINK_RELATION),
    ("accessibility/report.pdf/figure-review.csv", figures.REVIEW_LINK_RELATION),
    ("text/page-000/qwen-lines.json", ocr.LINES_LINK_RELATION),
    ("something/else.bin", None),
])
def test_relation_for(rel_path, relation):
    assert workspace._relation_for(rel_path) == relation


def test_sources_for_falls_back_to_every_binary_when_no_source_is_named():
    bronze = {"a.tiff": "cask:/b/a.tiff", "b.tiff": "cask:/b/b.tiff"}
    assert workspace._sources_for("split-spreads/gutters.json", bronze) == ["cask:/b/a.tiff", "cask:/b/b.tiff"]
    assert workspace._sources_for("split-spreads/pages/b_R.tiff", bronze) == ["cask:/b/b.tiff"]
    assert workspace._sources_for("split-spreads/pages/a.tiff", bronze) == ["cask:/b/a.tiff"]


def test_workspace_listings_request_one_large_page(monkeypatch):
    fake = _fake_ls({"cask:/workspace": []})
    monkeypatch.setattr(fs, "ls", fake)
    workspace.list_folders()
    assert fake.calls == [("cask:/workspace", {"limit": 10000})]


def test_apply_to_targets_many_items_is_partial_when_any_item_is(monkeypatch, tracked):
    monkeypatch.setattr(workspace, "resolve_targets", lambda target, opts={}: [WS_ITEM, IMAGES_ONLY_WS_ITEM])
    statuses = iter(["success", "partial"])

    result = workspace.apply_to_targets("cask:/workspace/jdoe/box-12", lambda item, opts: {"status": next(statuses)},
                                        opts={"stage": "ocr"})

    assert result["status"] == "partial"


def test_run_is_partial_when_any_step_is(monkeypatch, tracked):
    monkeypatch.setattr(workspace, "resolve_targets", lambda target, opts={}: [IMAGES_ONLY_WS_ITEM])
    monkeypatch.setitem(workspace.STEPS, "ocr", (lambda item, opts={}: {"status": "partial", "failed": [{}]}, {"image"}))

    result = workspace.run("cask:/workspace/jdoe/box-12", opts={"steps": ["ocr"]})

    assert result["status"] == "partial"


def test_run_keeps_going_when_a_step_fails(monkeypatch, fake_steps, tracked, capsys):
    monkeypatch.setattr(workspace, "resolve_targets", lambda target, opts={}: [WS_ITEM])

    def split_outage(item, opts={}):
        raise RuntimeError("Samwise chat completion failed after 6 tries: Connection error.")
    monkeypatch.setitem(workspace.STEPS, "split-detect", (split_outage, {"image"}))

    result = workspace.run("cask:/workspace/jdoe/box-12", opts={"steps": ["split-detect", "ocr", "alt-text", "tag-pdf"]})

    steps = result["items"][0]["steps"]
    assert steps["split-detect"] == {
        "status": "error", "error": "Samwise chat completion failed after 6 tries: Connection error.",
    }
    assert [name for name, _, _ in fake_steps] == ["ocr", "alt-text", "tag-pdf"]
    assert result["status"] == "partial"
    assert "split-detect failed, continuing with remaining steps" in capsys.readouterr().err
    assert [c["stage"] for c in tracked] == ["split-detect", "ocr", "alt-text", "tag-pdf"]


def test_run_raises_when_every_step_that_ran_failed(monkeypatch, tracked):
    monkeypatch.setattr(workspace, "resolve_targets", lambda target, opts={}: [IMAGES_ONLY_WS_ITEM])

    def down(item, opts={}):
        raise RuntimeError("Samwise down")
    for step in ("ocr", "alt-text"):
        monkeypatch.setitem(workspace.STEPS, step, (down, {"image", "pdf"}))

    # tag-pdf is skipped (no PDFs) and doesn't count as a success.
    with pytest.raises(RuntimeError, match=r"every step failed \(2\): Samwise down"):
        workspace.run("cask:/workspace/jdoe/box-12", opts={"steps": ["ocr", "alt-text", "tag-pdf"]})


# --- per-file runs / list_items -------------------------------------------------

def test_run_with_files_skips_folder_steps_and_passes_files_through(monkeypatch, fake_steps, tracked):
    monkeypatch.setattr(workspace, "resolve_targets", lambda target, opts={}: [WS_ITEM])

    result = workspace.run("cask:/workspace/jdoe/box-12/source/folder-3", opts={
        "steps": ["split-detect", "ocr", "alt-text", "tag-pdf"],
        "step_opts": {"ocr": {"vision": False}},
        "files": ["photo-a.tiff"],
    })

    steps = result["items"][0]["steps"]
    assert steps["split-detect"] == {"skipped": "split-spreads runs per folder, not per file"}
    assert steps["tag-pdf"] == {"skipped": "no pdf binaries"}  # photo-a.tiff alone has no PDF
    assert [(name, opts) for name, _, opts in fake_steps] == [
        ("ocr", {"vision": False, "files": {"photo-a.tiff"}}),
        ("alt-text", {"files": {"photo-a.tiff"}}),
    ]
    assert result["items"][0]["item"] == "cask:/workspace/jdoe/box-12/source/folder-3#photo-a.tiff"
    assert {c["item_id"] for c in tracked} == {"cask:/workspace/jdoe/box-12/source/folder-3#photo-a.tiff"}


def test_run_with_a_pdf_file_runs_tag_pdf(monkeypatch, fake_steps, tracked):
    monkeypatch.setattr(workspace, "resolve_targets", lambda target, opts={}: [WS_ITEM])

    workspace.run("x", opts={"steps": ["ocr", "tag-pdf"], "files": ["document.pdf"]})

    assert [name for name, _, _ in fake_steps] == ["ocr", "tag-pdf"]


def test_list_items_lists_each_folders_image_and_pdf_files(monkeypatch):
    monkeypatch.setattr(items, "list_workspace_items", lambda path: [
        "cask:/workspace/jdoe/box-12/source", "cask:/workspace/jdoe/box-12/source/folder-3",
    ])
    by_dir = {
        "cask:/workspace/jdoe/box-12/source": IMAGES_ONLY_WS_ITEM,
        "cask:/workspace/jdoe/box-12/source/folder-3": ItemLocation(
            ark=None, group="jdoe", collection="box-12", time=None, item_path="folder-3",
            bronze_binary_paths=[
                "cask:/workspace/jdoe/box-12/source/folder-3/a.pdf",
                "cask:/workspace/jdoe/box-12/source/folder-3/notes.txt",
                "cask:/workspace/jdoe/box-12/source/folder-3/p1.tif",
            ],
            dest_type="workspace", dest_prefix="derivatives",
        ),
    }
    monkeypatch.setattr(items, "resolve_workspace", lambda path: by_dir[path])

    assert workspace.list_items("cask:/workspace/jdoe/box-12") == [
        {"item": "cask:/workspace/jdoe/box-12/source", "files": ["cover.jpg"]},
        {"item": "cask:/workspace/jdoe/box-12/source/folder-3", "files": ["a.pdf", "p1.tif"]},
    ]
