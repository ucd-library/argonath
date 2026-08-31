import json
import uuid
from types import SimpleNamespace

import pytest

from digtk.lib import gcs_sync
from digtk.lib.items import ItemLocation

ARK = "ark:/87287/d7abc1234"

IMAGE_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tif"],
)

PDF_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/document.pdf"],
)

VIDEO_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=["cask:/bronze/g/c/latest/item/movie.mp4"],
)

MIXED_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo.tif",
        "cask:/bronze/g/c/latest/item/document.pdf",
    ],
)

# Multiple TIFF page masters alongside a PDF -- the real yearbooks-style
# shape that genuinely needs the 'media' wrapper (see _needs_media_wrapper()).
MULTI_IMAGE_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/photo1.tif",
        "cask:/bronze/g/c/latest/item/photo2.tif",
        "cask:/bronze/g/c/latest/item/document.pdf",
    ],
)

# A video with a single poster image -- the real punjabi-style shape
# (ark:/87293/d3361t) that must NOT get the 'media' wrapper, despite having
# more than one bronze binary.
VIDEO_WITH_POSTER_ITEM = ItemLocation(
    ark=ARK, group="g", collection="c", time="latest", item_path="item",
    bronze_binary_paths=[
        "cask:/bronze/g/c/latest/item/movie.mp4",
        "cask:/bronze/g/c/latest/item/poster.png",
    ],
)

IMAGE_MANIFEST = {
    "written_at": "2026-06-24T10:00:00Z",
    "status": "success",
    "command": ["digtk", "image", "derivatives", ARK],
    "params": {"widths": [256, 512, 1024]},
    "outputs": ["small.jpg", "medium.jpg", "large.jpg", "tiled.tif"],
    "duration_s": 1.0,
    "original": {"size": {"width": "2797", "height": "3561"}, "url": "cask://bronze/g/c/latest/item/photo.tif"},
    "small": {"size": {"width": "256", "height": "326"}, "url": "cask://silver/.../small.jpg"},
    "medium": {"size": {"width": "512", "height": "652"}, "url": "cask://silver/.../medium.jpg"},
    "large": {"size": {"width": "1024", "height": "1304"}, "url": "cask://silver/.../large.jpg"},
    "tiled": {"size": [{"width": "3072", "height": "3911"}], "url": "cask://silver/.../tiled.tif"},
    "ocr": {"size": {"width": "1024", "height": "1304"}, "imageSize": "large", "url": "cask://silver/.../ocr.djvu"},
}

VIDEO_MANIFEST = {
    "written_at": "2026-06-24T10:00:00Z",
    "status": "success",
    "command": ["digtk", "video", "derivatives", ARK],
    "params": {"resolutions": ["360p", "720p"]},
    "outputs": ["360p.m3u8", "360p.ts", "720p.m3u8", "720p.ts", "playlist.m3u8"],
    "duration_s": 1.0,
}


@pytest.fixture
def fake_upload(monkeypatch):
    calls = []
    monkeypatch.setattr(gcs_sync, "_upload", lambda bucket, path, data: calls.append((bucket, path, data)))
    return calls


@pytest.fixture
def fake_fs(monkeypatch):
    def fake_read(path):
        if path.endswith("manifest.json"):
            if "/images/1/" in path or path.endswith("/images/1"):
                return json.dumps({**IMAGE_MANIFEST, "page": 1}).encode()
            if "/images/2/" in path or path.endswith("/images/2"):
                return json.dumps({**IMAGE_MANIFEST, "page": 2}).encode()
            if "/streaming/" in path:
                return json.dumps(VIDEO_MANIFEST).encode()
            return json.dumps(IMAGE_MANIFEST).encode()
        return b"binary-bytes"

    def fake_ls(path, opts={}):
        return [
            SimpleNamespace(name="1", kind="directory"),
            SimpleNamespace(name="2", kind="directory"),
            SimpleNamespace(name="manifest.json", kind="file"),
        ]

    monkeypatch.setattr(gcs_sync.fs, "read", fake_read)
    monkeypatch.setattr(gcs_sync.fs, "ls", fake_ls)


@pytest.fixture
def fake_image_item(monkeypatch):
    monkeypatch.setattr(gcs_sync.items, "resolve", lambda ark, opts={}: IMAGE_ITEM)


@pytest.fixture
def fake_pdf_item(monkeypatch):
    monkeypatch.setattr(gcs_sync.items, "resolve", lambda ark, opts={}: PDF_ITEM)


@pytest.fixture
def fake_video_item(monkeypatch):
    monkeypatch.setattr(gcs_sync.items, "resolve", lambda ark, opts={}: VIDEO_ITEM)


@pytest.fixture
def fake_mixed_item(monkeypatch):
    monkeypatch.setattr(gcs_sync.items, "resolve", lambda ark, opts={}: MIXED_ITEM)


@pytest.fixture
def fake_multi_image_item(monkeypatch):
    monkeypatch.setattr(gcs_sync.items, "resolve", lambda ark, opts={}: MULTI_IMAGE_ITEM)


@pytest.fixture
def fake_video_with_poster_item(monkeypatch):
    monkeypatch.setattr(gcs_sync.items, "resolve", lambda ark, opts={}: VIDEO_WITH_POSTER_ITEM)


def test_fin_path_encodes_ark_and_filename():
    assert gcs_sync._fin_path(ARK, "photo.tif") == f"/item/{ARK}/photo.tif"


def test_fin_path_adds_media_segment_when_requested():
    assert gcs_sync._fin_path(ARK, "photo.tif", media=True) == f"/item/{ARK}/media/photo.tif"


def test_fin_path_adds_subcontainer_segment_when_requested():
    assert (
        gcs_sync._fin_path(ARK, "photo.tif", media=True, subcontainer="images")
        == f"/item/{ARK}/media/images/photo.tif"
    )


def test_fin_path_ignores_subcontainer_for_single_binary_item():
    # subcontainer only makes sense inside a 'media' container -- a single-
    # binary item attaches directly to the item resource, no wrapper at all.
    assert gcs_sync._fin_path(ARK, "photo.tif", media=False, subcontainer="images") == f"/item/{ARK}/photo.tif"


def test_needs_media_wrapper_true_for_multiple_images():
    assert gcs_sync._needs_media_wrapper(MULTI_IMAGE_ITEM) is True


def test_needs_media_wrapper_false_for_single_image_plus_pdf():
    # Confirmed against real production data (2026-08-27): a lone image
    # alongside a non-image binary (a poster, a transcript) isn't a
    # multi-page image group -- keyed on image-binary count specifically,
    # not total bronze-binary count.
    assert gcs_sync._needs_media_wrapper(MIXED_ITEM) is False


def test_needs_media_wrapper_false_for_video_with_poster():
    # The real punjabi-style shape (ark:/87293/d3361t) that motivated this
    # fix: 2 bronze binaries (video + poster), but only 1 is an image.
    assert gcs_sync._needs_media_wrapper(VIDEO_WITH_POSTER_ITEM) is False


def test_needs_media_wrapper_false_for_single_binary_item():
    assert gcs_sync._needs_media_wrapper(IMAGE_ITEM) is False


def test_dams_image_manifest_rewrites_product_urls():
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/photo.tif", gcs_subpath="images")

    assert dams["small"]["url"] == "/fcrepo/rest/item/ark/photo.tif/svc:gcs/{{BUCKET}}/images/small.jpg"
    assert dams["small"]["size"] == IMAGE_MANIFEST["small"]["size"]


def test_dams_image_manifest_adds_page_segment_and_page_field():
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/doc.pdf", gcs_subpath="images", page=3)

    assert dams["page"] == 3
    assert dams["small"]["url"] == "/fcrepo/rest/item/ark/doc.pdf/svc:gcs/{{BUCKET}}/images/3/small.jpg"


def test_dams_image_manifest_rewrites_original_to_fcrepo_binary():
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/photo.tif", gcs_subpath="images")
    assert dams["original"] == {"size": IMAGE_MANIFEST["original"]["size"], "url": "/fcrepo/rest/item/ark/photo.tif"}


def test_dams_image_manifest_adds_iiif_url_for_tiled():
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/photo.tif", gcs_subpath="images")
    assert dams["tiled"]["iiif"] == "/fcrepo/rest/item/ark/photo.tif/svc:iiif/images/tiled.tif"


def test_dams_image_manifest_drops_outputs():
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/photo.tif", gcs_subpath="images")
    assert "outputs" not in dams


def test_dams_image_manifest_passes_through_real_ocr_for_plain_images():
    # handleImageList() (client-media/model.js) builds a PDF+ImageList
    # item's book-reader page list synchronously from each individual
    # per-page TIFF's own clientMedia.images -- i.e. this function's
    # output when called from sync_image_item(), not sync_pdf_item() --
    # so 'ocr' must be populated here too, not just on PDF page manifests.
    # An earlier (stub-era) version only added it when page is not None,
    # which never reached this code path (confirmed against a real fin
    # import, 2026-08-27: the crash persisted even with a correct
    # PDF-page ocr stub already uploaded).
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/photo.tif", gcs_subpath="images")

    assert dams["ocr"]["size"] == IMAGE_MANIFEST["ocr"]["size"]
    assert dams["ocr"]["imageSize"] == IMAGE_MANIFEST["ocr"]["imageSize"]
    assert dams["ocr"]["url"] == "/fcrepo/rest/item/ark/photo.tif/svc:gcs/{{BUCKET}}/images/ocr.djvu"


def test_dams_image_manifest_reads_ocr_size_and_image_size_from_input_not_hardcoded():
    manifest = {**IMAGE_MANIFEST, "ocr": {"size": {"width": "512", "height": "652"}, "imageSize": "medium"}}
    dams = gcs_sync._dams_image_manifest(manifest, fin_path="/item/ark/photo.tif", gcs_subpath="images")
    assert dams["ocr"]["size"] == {"width": "512", "height": "652"}
    assert dams["ocr"]["imageSize"] == "medium"


def test_dams_image_manifest_omits_ocr_when_argonath_manifest_has_none():
    # An item not yet reprocessed with the real djvu OCR feature has no
    # "ocr" key in its own manifest.json at all -- there's no stub
    # fallback anymore (see this function's docstring), so the synced
    # manifest just omits the field rather than emitting a 404ing URL.
    manifest_without_ocr = {k: v for k, v in IMAGE_MANIFEST.items() if k != "ocr"}
    dams = gcs_sync._dams_image_manifest(manifest_without_ocr, fin_path="/item/ark/photo.tif", gcs_subpath="images")
    assert "ocr" not in dams


def test_dams_image_manifest_passes_through_real_ocr_for_pdf_pages():
    # BookReaderModel.js's _isValidPage() rejects any page missing
    # ocr.url/ocr.size outright -- with every page rejected, the book
    # reader's page list ends up empty and ucdlib-bookreader.js crashes
    # (confirmed against a real fin import, 2026-08-27: "Cannot read
    # properties of undefined (reading 'ele')"). digtk.models.digital_website.
    # pipeline.build_ocr_products() now produces a real djvu file to satisfy
    # that check with actual content, not a 404ing stub.
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/doc.pdf", gcs_subpath="images", page=3)

    assert dams["ocr"]["size"] == IMAGE_MANIFEST["ocr"]["size"]
    assert dams["ocr"]["imageSize"] == IMAGE_MANIFEST["ocr"]["imageSize"]
    assert dams["ocr"]["url"] == "/fcrepo/rest/item/ark/doc.pdf/svc:gcs/{{BUCKET}}/images/3/ocr.djvu"


def test_dams_image_manifest_matches_production_shape_exactly():
    # Confirmed against two real production clientMedia responses
    # (2026-08-26, ark:/87293/d3gq6r65h -- a single page, then the full
    # 128-page pdf.pages array): argonath's own bookkeeping fields
    # (written_at, params, command, status, duration_s) must not leak into
    # the client-facing manifest, and no product may carry a 'gcsPath' key
    # -- production objects never have either. Earlier versions of this
    # function did both.
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/photo.tif", gcs_subpath="images")
    assert set(dams.keys()) == {"original", "small", "medium", "large", "tiled", "ocr"}
    for leaked_field in ("written_at", "params", "command", "status", "duration_s"):
        assert leaked_field not in dams
    assert set(dams["small"].keys()) == {"size", "url"}
    assert set(dams["tiled"].keys()) == {"size", "url", "iiif"}
    assert set(dams["ocr"].keys()) == {"size", "imageSize", "url"}


def test_dams_image_manifest_matches_production_shape_exactly_for_pdf_page():
    dams = gcs_sync._dams_image_manifest(IMAGE_MANIFEST, fin_path="/item/ark/doc.pdf", gcs_subpath="images", page=0)
    assert set(dams.keys()) == {"page", "original", "small", "medium", "large", "tiled", "ocr"}
    assert set(dams["ocr"].keys()) == {"size", "imageSize", "url"}


def test_workflow_marker_is_completed_and_deterministic():
    marker_a = gcs_sync._workflow_marker(
        fin_path="/item/ark/photo.tif", bucket="dams-client-media-argonath",
        gcs_subpath="images", workflow_name="image-products",
    )
    marker_b = gcs_sync._workflow_marker(
        fin_path="/item/ark/photo.tif", bucket="dams-client-media-argonath",
        gcs_subpath="images", workflow_name="image-products",
    )

    assert marker_a["state"] == "completed"
    assert marker_a["name"] == "image-products"
    assert marker_a["data"]["finPath"] == "/item/ark/photo.tif"
    assert marker_a["data"]["gcsBucket"] == "dams-client-media-argonath"
    assert marker_a["workflow_id"] == marker_b["workflow_id"]


def test_workflow_marker_sets_notify_on_success():
    # Without this, workflow-model.js's notifyOnSuccess() silently no-ops and
    # fin never re-indexes the item after loading the marker -- clientMedia
    # stays empty forever (confirmed against a real fin import). Value must
    # match dams/services/init/fcrepo/fin/workflows/config.json's
    # defaults.notifyOnSuccess exactly.
    marker = gcs_sync._workflow_marker(
        fin_path="/item/ark/photo.tif", bucket="b", gcs_subpath="images", workflow_name="image-products",
    )
    assert marker["data"]["notifyOnSuccess"] == "/svc:reindex?no-crawl=true"


def test_workflow_marker_sets_a_non_empty_tmp_gcs_path():
    # workflow-model.js's _updateWorkflowBuckets() unconditionally does
    # `workflow.data.tmpGcsPath.replace(...)` with no null-check -- omitting
    # this 500s every clientMedia fetch, which dbsync treats as "inaccessible"
    # and deletes the binary from the ES index entirely (confirmed against a
    # real fin import: TypeError: Cannot read properties of undefined
    # (reading 'replace') at FinGcWorkflowModel._updateWorkflowBuckets).
    marker = gcs_sync._workflow_marker(
        fin_path="/item/ark/photo.tif", bucket="b", gcs_subpath="images", workflow_name="image-products",
    )
    assert marker["data"]["tmpGcsPath"].startswith("gs://")
    assert marker["data"]["tmpGcsBucket"]


def test_workflow_marker_differs_by_fin_path():
    marker_a = gcs_sync._workflow_marker(
        fin_path="/item/ark/a.tif", bucket="b", gcs_subpath="images", workflow_name="image-products",
    )
    marker_b = gcs_sync._workflow_marker(
        fin_path="/item/ark/b.tif", bucket="b", gcs_subpath="images", workflow_name="image-products",
    )
    assert marker_a["workflow_id"] != marker_b["workflow_id"]


def test_workflow_marker_id_is_a_bare_uuid():
    # fin's `workflow` Postgres table types workflow_id as a real `uuid`
    # column -- any prefix (e.g. "argonath-sync-{uuid}") 500s on insert with
    # "invalid input syntax for type uuid" (confirmed against a real fin import).
    marker = gcs_sync._workflow_marker(
        fin_path="/item/ark/photo.tif", bucket="b", gcs_subpath="images", workflow_name="image-products",
    )
    parsed = uuid.UUID(marker["workflow_id"])
    assert str(parsed) == marker["workflow_id"]
    assert marker["id"] == marker["workflow_id"]


def test_sync_ocr_outputs_uploads_djvu_and_stats_under_unprefixed_names(monkeypatch, fake_upload):
    def fake_read(path):
        if path.endswith("manifest.json"):
            return json.dumps({"outputs": ["prep.jpg", "ocr.djvu", "ocr-stats.json"]}).encode()
        return b"ocr-bytes"
    monkeypatch.setattr(gcs_sync.fs, "read", fake_read)

    gcs_sync._sync_ocr_outputs(
        IMAGE_ITEM, ocr_rel_dir="ocr", stem="photo", multi=False,
        gcs_prefix="item/ark/photo.tif/images", bucket="b",
    )

    uploaded_paths = [path for _, path, _ in fake_upload]
    assert "item/ark/photo.tif/images/ocr.djvu" in uploaded_paths
    assert "item/ark/photo.tif/images/ocr-stats.json" in uploaded_paths
    assert not any(p.endswith("prep.jpg") for p in uploaded_paths)


def test_sync_ocr_outputs_resolves_multi_source_prefixed_filenames(monkeypatch, fake_upload):
    # A shared multi-source ocr/ dir names files via output_name(), e.g.
    # "photo-a-ocr.djvu" -- these still upload under the plain unprefixed
    # GCS filename, matching production's per-source layout.
    def fake_read(path):
        if path.endswith("manifest.json"):
            return json.dumps({
                "outputs": ["photo-a-ocr.djvu", "photo-a-ocr-stats.json", "photo-b-ocr.djvu", "photo-b-ocr-stats.json"],
            }).encode()
        return b"ocr-bytes"
    monkeypatch.setattr(gcs_sync.fs, "read", fake_read)

    gcs_sync._sync_ocr_outputs(
        IMAGE_ITEM, ocr_rel_dir="ocr", stem="photo-a", multi=True,
        gcs_prefix="item/ark/photo-a.tif/images", bucket="b",
    )

    uploaded_paths = [path for _, path, _ in fake_upload]
    assert "item/ark/photo-a.tif/images/ocr.djvu" in uploaded_paths
    assert "item/ark/photo-a.tif/images/ocr-stats.json" in uploaded_paths
    assert not any("photo-b" in p for p in uploaded_paths)


def test_sync_ocr_outputs_skips_silently_when_files_not_yet_published(monkeypatch, fake_upload):
    # Reads the expected filenames directly rather than trusting the ocr
    # dir's own manifest.json's "outputs" list at all -- see this function's
    # docstring for why that list can't be trusted for a mixed-format item.
    def fake_read(path):
        raise FileNotFoundError(f'"{path}" does not exist')  # nothing published yet
    monkeypatch.setattr(gcs_sync.fs, "read", fake_read)

    gcs_sync._sync_ocr_outputs(
        IMAGE_ITEM, ocr_rel_dir="ocr", stem="photo", multi=False,
        gcs_prefix="item/ark/photo.tif/images", bucket="b",
    )
    assert fake_upload == []


def test_sync_ocr_outputs_ignores_a_stale_or_colliding_outputs_list(monkeypatch, fake_upload):
    # Confirmed via a real derivatives run (2026-08-28) against a mixed
    # image+PDF item: image.py's and pdf.py's shared ocr/ manifest.json can
    # get overwritten by whichever media type's derivatives ran last, even
    # though every individual product file is untouched. The ocr dir's own
    # manifest.json here claims totally unrelated "outputs" -- upload must
    # still happen because the actual djvu/stats files are readable.
    def fake_read(path):
        if path.endswith("manifest.json"):
            return json.dumps({"outputs": ["1/", "2/", "3/"]}).encode()  # stale/colliding
        return b"ocr-bytes"
    monkeypatch.setattr(gcs_sync.fs, "read", fake_read)

    gcs_sync._sync_ocr_outputs(
        IMAGE_ITEM, ocr_rel_dir="ocr", stem="photo", multi=False,
        gcs_prefix="item/ark/photo.tif/images", bucket="b",
    )

    uploaded_paths = [path for _, path, _ in fake_upload]
    assert "item/ark/photo.tif/images/ocr.djvu" in uploaded_paths
    assert "item/ark/photo.tif/images/ocr-stats.json" in uploaded_paths


def test_sync_outputs_tolerates_ocr_dir_never_published(fake_image_item, fake_upload, monkeypatch):
    # An item processed before this feature existed has no ocr.djvu/
    # ocr-stats.json at all -- _sync_outputs() must still succeed for its
    # real products.
    def fake_read(path):
        if path.endswith("manifest.json") and "/ocr" not in path:
            return json.dumps(IMAGE_MANIFEST).encode()
        if "/ocr" in path:
            raise FileNotFoundError(f'"{path}" does not exist')
        return b"binary-bytes"
    monkeypatch.setattr(gcs_sync.fs, "read", fake_read)

    result = gcs_sync._sync_outputs(
        IMAGE_ITEM, rel_dir="public-frontend-derivatives/images/photo.tif/images",
        fin_path="/item/ark/photo.tif", gcs_subpath="images", bucket="b",
        ocr_rel_dir="ocr", stem="photo", multi=False,
    )
    assert result["manifest"] == IMAGE_MANIFEST
    assert not any("ocr" in path for _, path, _ in fake_upload)


def test_sync_image_item_uploads_products_manifest_and_marker(fake_fs, fake_image_item, fake_upload):
    result = gcs_sync.sync_image_item(ARK, opts={"bucket": "dams-client-media-argonath"})

    uploaded_paths = [path for _, path, _ in fake_upload]
    prefix = f"item/{ARK}/photo.tif/images"
    for filename in ("small.jpg", "medium.jpg", "large.jpg", "tiled.tif", "manifest.json"):
        assert f"{prefix}/{filename}" in uploaded_paths
    assert f"workflows/item/{ARK}/photo.tif/image-products.json" in uploaded_paths
    assert all(bucket == "dams-client-media-argonath" for bucket, _, _ in fake_upload)
    assert result["images"]["photo.tif"]["finPath"] == f"/item/{ARK}/photo.tif"


def test_sync_image_item_manifest_upload_is_dams_shaped(fake_fs, fake_image_item, fake_upload):
    gcs_sync.sync_image_item(ARK, opts={"bucket": "b"})

    manifest_upload = next(data for _, path, data in fake_upload if path.endswith("/images/manifest.json"))
    manifest = json.loads(manifest_upload)
    assert manifest["small"]["url"] == f"/fcrepo/rest/item/{ARK}/photo.tif/svc:gcs/{{{{BUCKET}}}}/images/small.jpg"
    # 'ocr' is translated by _dams_image_manifest() into a real
    # /fcrepo/rest svc:gcs URL -- argonath's own source manifest's raw
    # cask:// ocr entry (see IMAGE_MANIFEST) must not leak through untranslated.
    assert manifest["ocr"]["imageSize"] == "large"
    assert manifest["ocr"]["url"] == f"/fcrepo/rest/item/{ARK}/photo.tif/svc:gcs/{{{{BUCKET}}}}/images/ocr.djvu"


def test_sync_image_item_uploads_real_ocr_djvu_and_stats(fake_image_item, fake_upload, monkeypatch):
    def fake_read(path):
        if path.endswith("manifest.json"):
            if "/ocr" in path:
                return json.dumps({"outputs": ["prep.jpg", "ocr.djvu", "ocr-stats.json"]}).encode()
            return json.dumps(IMAGE_MANIFEST).encode()
        return b"binary-bytes"
    monkeypatch.setattr(gcs_sync.fs, "read", fake_read)

    gcs_sync.sync_image_item(ARK, opts={"bucket": "b"})

    uploaded_paths = [path for _, path, _ in fake_upload]
    prefix = f"item/{ARK}/photo.tif/images"
    assert f"{prefix}/ocr.djvu" in uploaded_paths
    assert f"{prefix}/ocr-stats.json" in uploaded_paths
    assert not any("prep.jpg" in p for p in uploaded_paths)


def test_sync_pdf_item_syncs_every_page_and_aggregate_manifest(fake_fs, fake_pdf_item, fake_upload):
    result = gcs_sync.sync_pdf_item(ARK, opts={"bucket": "b"})

    uploaded_paths = [path for _, path, _ in fake_upload]
    base = f"item/{ARK}/document.pdf/images"
    # Storage directories are 0-indexed (page 1 -> "0", page 2 -> "1") --
    # see test_sync_pdf_item_first_page_lands_at_storage_index_zero below.
    assert f"{base}/0/small.jpg" in uploaded_paths
    assert f"{base}/1/small.jpg" in uploaded_paths
    assert f"{base}/0/manifest.json" in uploaded_paths
    assert f"{base}/manifest.json" in uploaded_paths  # aggregate
    assert f"workflows/item/{ARK}/document.pdf/pdf-image-products.json" in uploaded_paths
    assert result["pdfs"]["document.pdf"]["pages"] == 2


def test_sync_pdf_item_aggregate_manifest_lists_pages_sorted(fake_fs, fake_pdf_item, fake_upload):
    gcs_sync.sync_pdf_item(ARK, opts={"bucket": "b"})

    aggregate = next(
        json.loads(data) for _, path, data in fake_upload
        if path == f"item/{ARK}/document.pdf/images/manifest.json"
    )
    # Display "page" values are 0-indexed, matching a real production
    # clientMedia response (its first page reads "page": 0) -- confirmed
    # 2026-08-26 against ark:/87293/d3gq6r65h. Both storage path *and*
    # display field are 0-indexed; argonath's own 1-indexed source page
    # directories (1, 2, ...) never leak into the synced output.
    assert [p["page"] for p in aggregate["pages"]] == [0, 1]


def test_sync_pdf_item_first_page_lands_at_storage_index_zero(fake_fs, fake_pdf_item, fake_upload):
    # dams/services/fin/models/item/transform.js hardcodes a fetch to
    # '{finPath}/images/0/manifest.json' as a general clientMedia preview for
    # the PDF as a whole -- confirmed against a real fin import (2026-08-26)
    # that this 404s when storage uses argonath's own 1-indexed page dirs.
    # The manifest CONTENT's own "page" field is 0-indexed too, matching a
    # real production clientMedia response.
    gcs_sync.sync_pdf_item(ARK, opts={"bucket": "b"})

    first_page_manifest = next(
        json.loads(data) for _, path, data in fake_upload
        if path == f"item/{ARK}/document.pdf/images/0/manifest.json"
    )
    assert first_page_manifest["page"] == 0
    assert first_page_manifest["small"]["url"].endswith("/images/0/small.jpg")


def test_sync_video_item_uploads_stream_files_but_no_manifest(fake_fs, fake_video_item, fake_upload):
    result = gcs_sync.sync_video_item(ARK, opts={"bucket": "b"})

    uploaded_paths = [path for _, path, _ in fake_upload]
    prefix = f"item/{ARK}/movie.mp4/streaming"
    for filename in VIDEO_MANIFEST["outputs"]:
        assert f"{prefix}/{filename}" in uploaded_paths
    assert f"{prefix}/manifest.json" not in uploaded_paths
    assert f"workflows/item/{ARK}/movie.mp4/video-to-stream.json" in uploaded_paths
    assert result["videos"]["movie.mp4"]["finPath"] == f"/item/{ARK}/movie.mp4"


def test_sync_item_only_calls_matching_media_type(fake_fs, fake_image_item, fake_upload):
    result = gcs_sync.sync_item(ARK, opts={"bucket": "b"})
    assert "images" in result
    assert "pdfs" not in result
    assert "videos" not in result


def test_sync_item_handles_mixed_format_item(fake_fs, fake_mixed_item, fake_upload):
    result = gcs_sync.sync_item(ARK, opts={"bucket": "b"})
    assert "images" in result
    assert "pdfs" in result
    assert "videos" not in result


def test_sync_item_adds_media_segment_for_multi_image_item(fake_fs, fake_multi_image_item, fake_upload):
    # Genuinely needs the wrapper: more than one TIFF page master alongside
    # a PDF (the real yearbooks-style shape). TIFF images nest one level
    # deeper than the PDF -- under their own 'images' DirectContainer, not
    # flat under 'media' (confirmed against a real fin import: getting this
    # wrong 404s clientMedia for every image even though the PDF, with no
    # extra segment, works fine).
    result = gcs_sync.sync_item(ARK, opts={"bucket": "b"})

    assert result["images"]["photo1.tif"]["finPath"] == f"/item/{ARK}/media/images/photo1.tif"
    assert result["images"]["photo2.tif"]["finPath"] == f"/item/{ARK}/media/images/photo2.tif"
    assert result["pdfs"]["document.pdf"]["finPath"] == f"/item/{ARK}/media/document.pdf"
    uploaded_paths = [path for _, path, _ in fake_upload]
    assert f"item/{ARK}/media/images/photo1.tif/images/small.jpg" in uploaded_paths
    assert f"item/{ARK}/media/document.pdf/images/1/small.jpg" in uploaded_paths


def test_sync_item_omits_media_segment_for_single_image_plus_pdf_item(fake_fs, fake_mixed_item, fake_upload):
    # A single image alongside a PDF (e.g. a poster + a transcript) is NOT
    # a multi-page image group -- confirmed against real production data
    # (2026-08-27): the wrapper is keyed on image-binary count, not total
    # bronze-binary count. An earlier version of _needs_media_wrapper()'s
    # logic (len(bronze_binary_paths) > 1) wrongly wrapped this shape.
    result = gcs_sync.sync_item(ARK, opts={"bucket": "b"})

    assert result["images"]["photo.tif"]["finPath"] == f"/item/{ARK}/photo.tif"
    assert result["pdfs"]["document.pdf"]["finPath"] == f"/item/{ARK}/document.pdf"


def test_sync_item_omits_media_segment_for_video_with_poster_item(fake_fs, fake_video_with_poster_item, fake_upload):
    # The real production shape (ark:/87293/d3361t) that motivated this
    # fix: a video + a single poster image, both attaching directly at the
    # item root, no 'media' wrapper for either, despite 2 bronze binaries.
    result = gcs_sync.sync_item(ARK, opts={"bucket": "b"})

    assert result["videos"]["movie.mp4"]["finPath"] == f"/item/{ARK}/movie.mp4"
    assert result["images"]["poster.png"]["finPath"] == f"/item/{ARK}/poster.png"


def test_sync_image_item_omits_media_segment_for_single_binary_item(fake_fs, fake_image_item, fake_upload):
    result = gcs_sync.sync_image_item(ARK, opts={"bucket": "b"})
    assert result["images"]["photo.tif"]["finPath"] == f"/item/{ARK}/photo.tif"


def test_sync_item_skips_media_type_with_no_derivatives_yet(fake_mixed_item, monkeypatch, fake_upload):
    # Mixed image+PDF item where only `digtk public-dc image derivatives` has been run --
    # the PDF's silver process directory doesn't exist yet, so fs.ls() 404s
    # (matches CaskBackend's documented 404 -> FileNotFoundError mapping).
    def fake_read(path):
        return json.dumps(IMAGE_MANIFEST).encode()

    def fake_ls(path, opts={}):
        if "document.pdf" in path:
            raise FileNotFoundError(f'Directory "{path}" does not exist')
        return []

    monkeypatch.setattr(gcs_sync.fs, "read", fake_read)
    monkeypatch.setattr(gcs_sync.fs, "ls", fake_ls)

    result = gcs_sync.sync_item(ARK, opts={"bucket": "b"})

    assert result["status"] == "success"
    assert "images" in result
    assert "pdfs" not in result
    assert "pdf" in result["skipped"]
    assert "digtk public-dc pdf derivatives" in result["skipped"]["pdf"]


def test_sync_item_raises_on_unrecognized_extension(fake_fs, monkeypatch, fake_upload):
    bad_item = ItemLocation(
        ark=ARK, group="g", collection="c", time="latest", item_path="item",
        bronze_binary_paths=["cask:/bronze/g/c/latest/item/photo.tif", "cask:/bronze/g/c/latest/item/mystery.xyz"],
    )
    monkeypatch.setattr(gcs_sync.items, "resolve", lambda ark, opts={}: bad_item)

    with pytest.raises(ValueError, match="unrecognized media extension"):
        gcs_sync.sync_item(ARK, opts={"bucket": "b"})
