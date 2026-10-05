"""
digtk.lib.gcs_sync -- sync argonath-generated derivatives into the legacy
DAMS/fin GCS bucket layout, so a fin collection-import run recognizes them as
already generated and skips re-running its own Cloud Workflow derivative
pipeline (image-utils, triggered via fin's Google Cloud Workflows
integration) for items argonath has already processed.

Read-only against CaskFS -- this only ever calls digtk.lib.fs.read() (see
CLAUDE.md's digtk.lib.fs entry); it never writes back to CaskFS, so it
doesn't go through digtk.lib.derivative. All writes are to GCS, via the
standard google-cloud-storage client, authenticated the normal Application
Default Credentials way (`gcloud auth application-default login` locally).
Running this automatically in-cluster (a dedicated service account /
workload identity for the Dagster pipeline) is a deliberate follow-up, not
wired up here.

Path/shape conventions below were reverse-engineered from the dams/fin repos
rather than from a spec, since there isn't one:
  - dams/services/fin/models/workflows.js (AUTO_WORKFLOWS names, the
    node.clientMedia short-circuit)
  - dams/services/fin/models/item/transform.js (how a synced manifest.json
    gets read back into an item's clientMedia)
  - dams/services/image-utils/lib/image-manifest-to-dams-manifest.js and
    lib/workflow-service-utils.js (the DAMS manifest.json shape + gcs path
    formula)
  - fin/services/fin/node-utils/lib/gc/workflow-model.js and
    lib/workflow/postgres.js (the workflow-completion marker JSON /
    Postgres `workflow` row shape that createWorkflow()/workflowExistsInGcs()
    bootstrap from)

The workflow-completion marker shape (_workflow_marker()) has now been
execution-tested end-to-end against a real fin import (2026-08-25 and
2026-08-26, the yearbooks-argonath item), diffed directly against a real
production clientMedia response for the same source item (ark:/87293/
d3gq6r65h) to confirm the synced shape matches exactly. Ten bugs surfaced
and were fixed along the way:
  - workflow_id must be a bare UUID string (fin's `workflow` Postgres table
    types it as a real `uuid` column; a prefixed id like "argonath-sync-{uuid}"
    500s on insert).
  - TIFF image binaries in a multi-binary item need an extra 'images'
    subcontainer segment (/media/images/{filename}), not just /media/{filename}
    -- a compiled PDF doesn't need this, which is why the PDF worked before
    images did.
  - data.notifyOnSuccess must be set to "/svc:reindex?no-crawl=true" (matching
    dams/services/init/fcrepo/fin/workflows/config.json's
    defaults.notifyOnSuccess) or fin's workflow-model.js silently no-ops the
    re-index step, leaving clientMedia permanently empty even on a from-scratch
    import with GCS already populated beforehand -- dams/services/fin/models/
    item/model.js's update() always indexes *before* auto-triggering the
    workflow, so the first (only, without this) index pass never sees
    clientMedia regardless of sync timing.
  - data.tmpGcsPath must be a non-empty string (any 'gs://...' value; its
    real content is never read for a synthetic already-completed marker).
    workflow-model.js's _updateWorkflowBuckets() -- called unconditionally
    by getWorkflowInfo(), which transform.js's clientMedia-population code
    calls once notifyOnSuccess (above) is actually wired up -- does
    `workflow.data.tmpGcsPath.replace(...)` with no null-check. Omitting it
    500s *every* clientMedia fetch, and dbsync treats a 500 as "resource
    inaccessible" and deletes the binary from the ES index entirely -- worse
    than the missing-notifyOnSuccess symptom (an empty clientMedia field),
    this makes the whole node vanish from the item's @graph.
  - A PDF's page numbers must be 0-indexed (see sync_pdf_item()'s
    docstring), not argonath's own 1-indexed page numbers -- confirmed two
    ways: transform.js hardcodes a fetch to '.../images/0/manifest.json' as
    a general clientMedia preview for the PDF as a whole (so the *storage*
    path must be 0-indexed), and a real production clientMedia response
    shows the *display* 'page' field is also 0-indexed (its first page
    reads "page": 0) -- an earlier version of this fix got the storage path
    right but kept the display field 1-indexed, which was wrong.
  - _dams_image_manifest() must build an explicit clean dict, not copy
    argonath_manifest's own bookkeeping fields (written_at, params, command,
    status, duration_s) through into the client-facing shape -- a real
    production clientMedia object never has these; it's exactly
    {original?, small, medium, large, tiled(+iiif), page?, extractDensity?,
    ocr?}. Harmless in isolation (extra fields, not missing ones) but
    confirmed as a real mismatch by diffing against production directly.
  - Each product entry (small/medium/large/tiled) must be exactly {size,
    url} (tiled adds iiif) -- no 'gcsPath' field. An earlier version added
    a 'gcsPath' per product, reverse-engineered from
    workflow-service-utils.js's getGcsBasePath() formula and assumed to be
    part of the client-facing shape; a second full production diff
    (2026-08-26, the same item's complete 128-page pdf.pages array)
    confirmed real clientMedia responses never carry it -- whatever
    internal use getGcsBasePath() serves stays inside image-utils and never
    reaches the manifest.json a client actually reads.
  - Every product manifest with a 'large' entry needs a stub 'ocr' field
    ({size, imageSize, url}), even though argonath doesn't produce real
    djvu OCR content. client/public/lib/models/BookReaderModel.js's
    _isValidPage() rejects any page missing ocr.url/ocr.size outright;
    with every page rejected, the book reader's page list is empty and
    ucdlib-bookreader.js's _renderCurrentPages() crashes ("Cannot read
    properties of undefined (reading 'ele')") trying to look up a page
    that was never registered. Confirmed against a real fin import
    (2026-08-27) and against production's own PDF pages, which always
    carry a real ocr.djvu entry. The stub URL here 404s if actually
    fetched -- OCR text/search stays a known, separate gap -- it exists
    only to satisfy the page-validity check so images render at all.
    Critically, for a PDF+ImageList item the book reader never reads the
    PDF's own aggregate manifest at all --
    client-media/model.js's handleImageList() builds the page list
    synchronously from each individual per-page TIFF binary's own
    clientMedia.images (sync_image_item()'s output), bypassing
    sync_pdf_item()'s manifest and the async loadManifests()/BookReaderModel
    PDF-loading path entirely. An earlier version of this fix added the
    ocr stub only to sync_pdf_item()'s per-page manifests, which the
    crashing code path never reads -- confirmed via a real fin import
    (2026-08-27) that the crash persisted unchanged even after the PDF
    page manifests were verified, via direct GCS fetch, to carry a
    correct ocr stub. Fixed by adding the stub unconditionally in
    _dams_image_manifest(), so it lands in both sync_image_item()'s and
    sync_pdf_item()'s output.
  - The 'media' fcrepo wrapper isn't simply "this item has more than one
    bronze binary" -- confirmed by sampling ~15 real multi-binary items
    across dams-collections/ (2026-08-27). It's only needed when a role
    groups *multiple binaries of the same kind* (a PDF's multi-page TIFF
    masters); an item whose binaries each play a distinct singular role (a
    video + its poster image, or a video + transcript PDF + poster +
    captions) attaches every one directly at the item root, no wrapper,
    regardless of total binary count. The earlier `len(bronze_binary_paths)
    > 1` heuristic wrongly wrapped a video+poster-image item's video and
    poster both under '/media/', which doesn't exist in the real fcrepo
    tree at all for that item shape. See _needs_media_wrapper() and
    _fin_path()'s docstring.

A stray macOS '.DS_Store' file that leaks into a collection's fin-export
source tree (dams-collections/<name>/item/.../media/images/.DS_Store) is a
related but separate failure mode worth noting here, even though it isn't
an argonath/gcs_sync bug: fin's DirectContainer machinery picks it up as a
member alongside real page binaries, giving it a 'schema:hasPart' entry
with no corresponding clientMedia. The frontend's buildIaReaderPages()
(ucd-lib-client's lib/utils/index.js) then logs "no clientMedia images for
undefined" for that entry. `fin io import .` won't prune it on its own
(it defaults to ignoreRemoval: true even after deleting the file from
disk) -- removing the stray fcrepo resource directly (e.g. `fin http
delete <path>`) is required.

The djvu OCR-text-layer product (legacy manifest key "ocr") beyond the
stub above is not implemented -- argonath doesn't produce djvu today, only
Tesseract hocr/txt. Audio isn't synced at all -- fin's AUTO_WORKFLOWS has
no audio entry, so there's no legacy audio-derivative check to satisfy.
"""
import json
import os
import uuid
from datetime import datetime, timezone

from digtk.lib import fs, items
from digtk.lib.config import CONFIG
from digtk.models.digital_website import pipeline

# fin's AUTO_WORKFLOWS name per media type (dams/services/fin/models/workflows.js) --
# what a completion marker's "name" field must match for fin's
# workflowExistsInGcs()/autoTriggerWorkflow() to recognize it.
WORKFLOW_NAME = {
    "image": "image-products",
    "pdf": "pdf-image-products",
    "video": "video-to-stream",
}

# fin's gcsSubpath per workflow (dams/services/init/fcrepo/fin/workflows/config.json) --
# the directory segment under a binary's finPath that products land in.
GCS_SUBPATH = {"image": "images", "pdf": "images", "video": "streaming"}

# dams/services/image-utils/lib/config.js's image.products, minus "ocr" (the
# djvu OCR-text layer -- see module docstring). Maps each manifest key to the
# fixed filename argonath already publishes it under (matches the legacy
# names exactly, so no renaming is needed, only the manifest.json content
# that references them).
_IMAGE_PRODUCT_FILENAME = {
    "small": "small.jpg", "medium": "medium.jpg", "large": "large.jpg", "tiled": "tiled.tif",
}

_client = None


def _gcs_client():
    """Lazily builds the process-wide GCS client.

    Import kept lazy so importing digtk.lib.gcs_sync doesn't require
    google-cloud-storage to be installed unless a sync actually runs.

    :returns: Authenticated GCS client (Application Default Credentials).
    :rtype: google.cloud.storage.Client
    """
    global _client
    if _client is None:
        from google.cloud import storage
        _client = storage.Client()
    return _client


_CONTENT_TYPE_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".json": "application/json", ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t", ".m4s": "video/iso.segment", ".mp4": "video/mp4",
}


def _content_type_for(filename):
    """
    :param str filename:
    :returns: Guessed MIME type, or None if the extension isn't recognized.
    :rtype: str|None
    """
    return _CONTENT_TYPE_BY_EXT.get(os.path.splitext(filename)[1].lower())


def _upload(bucket_name, blob_path, data):
    """Uploads bytes to one GCS object.

    :param str bucket_name: Target bucket.
    :param str blob_path: Object path within the bucket (no leading slash, no gs:// scheme).
    :param bytes data: Content to upload.
    """
    blob = _gcs_client().bucket(bucket_name).blob(blob_path)
    blob.upload_from_string(data, content_type=_content_type_for(blob_path))


def _fin_path(ark, filename, *, media=False, subcontainer=None):
    """Builds the fcrepo container path fin uses for one of an item's binaries.

    A real fin item attaches a *single* binary directly to the item resource
    (`/item/{ark}/{filename}`), but when an item groups more than one binary
    (e.g. per-page TIFF masters alongside a compiled PDF, sharing one ark --
    see docs/cask-conventions.md's "Multi-Format Items"), there's no single
    "primary" binary to attach directly -- those live instead under a child
    `media` DirectContainer (`schema:associatedMedia`/`encodesCreativeWork`
    relations; see fin-api's `fin mkdir -T media` in
    fin/fin-api/cli/lib/cli/LocationCli.js), giving `/item/{ark}/media/{filename}`.

    Within that `media` container, a compiled document (a PDF) attaches
    directly -- but its per-page image masters live one level deeper, under
    their own `images` DirectContainer (`ucdlib:ImageList`, grouping them via
    `schema:hasPart`/`encodesCreativeWork` back to the PDF -- see
    docs/cask-conventions.md's "Multi-Format Items" and the real fin export at
    dams-collections/yearbooks/item/ark:/424242/d3gq6r65h/media/images/), giving
    `/item/{ark}/media/images/{filename}`.

    This is a structural inference (fin-api never asserts "media"/"images"
    from code -- it's purely how each collection's source metadata git repo
    happened to lay binaries out on disk, mechanically mirrored into fcrepo
    paths on import; see sync_item()'s docstring) confirmed against one real
    fin import (2026-08-25, the yearbooks-argonath item): a wrong path here
    (missing the "images" segment) 404'd fin's clientMedia lookup, which in
    turn caused fin's dbsync to auto-trigger regeneration for every TIFF even
    though the completion marker had synced correctly -- the PDF, which
    doesn't need this extra segment, worked first try. A single-binary item
    was confirmed to have no `media` segment at all.

    Whether an item needs the `media` wrapper is NOT simply "more than one
    bronze binary" -- confirmed by sampling ~15 real multi-binary items
    across dams-collections/ (2026-08-27): the wrapper appears only when a
    role needs to group *multiple binaries of the same kind* (a PDF's
    multi-page TIFF masters -- yearbooks, tulare, pdf-download, robinson).
    An item whose binaries each play a distinct singular role (a video +
    its poster image, or a video + transcript PDF + poster + captions --
    multimedia, punjabi, medical-pathologies, corn, access-testing) attaches
    every one directly at the item root, no wrapper, regardless of total
    binary count -- see _needs_media_wrapper(). Two single-binary
    counter-examples were found still wrapped for no discernible reason
    (ark-fix-12-2025, dup-fedora-links -- likely collection-specific import
    anomalies from ARK-fix/dedup remediation work); not resolved here.

    :param str ark: Item ARK, e.g. 'ark:/87287/d7abc1234'.
    :param str filename: Bronze binary's own filename.
    :param bool media: True if this item has a multi-image page group (see
        _needs_media_wrapper()); False otherwise, including for a video or
        a lone poster/transcript binary regardless of how many other bronze
        binaries the item has.
    :param str|None subcontainer: Extra path segment between 'media' and
        filename, e.g. 'images' for a compound item's TIFF page masters.
        Only meaningful when media=True.
    :returns: fcrepo path, e.g. '/item/ark:/87287/d7abc1234/photo.tif' (single-
        binary), '/item/ark:/87287/d7abc1234/media/document.pdf' (multi-binary,
        no subcontainer), or '/item/ark:/87287/d7abc1234/media/images/photo.tif'
        (multi-binary, subcontainer='images').
    :rtype: str
    """
    if not media:
        return f"/item/{ark}/{filename}"
    subcontainer_segment = f"/{subcontainer}" if subcontainer else ""
    return f"/item/{ark}/media{subcontainer_segment}/{filename}"


def _needs_media_wrapper(item):
    """Whether this item's image/PDF binaries need the 'media' fcrepo
    wrapper -- see _fin_path()'s docstring for the evidence. Keyed on image
    binary count specifically, not total bronze binary count: a multi-page
    TIFF group needs the wrapper (and its own 'images' subcontainer), but a
    lone poster/cover image sitting alongside a video or PDF does not, no
    matter how many other bronze binaries the item has.

    Doesn't use pipeline.filter_binaries_by_format() -- it raises when zero
    binaries match, which is a normal, expected case here (an item with no
    images at all, e.g. a lone video).

    :param digtk.lib.items.ItemLocation item:
    :returns: True if this item has more than one image-format bronze binary.
    :rtype: bool
    """
    image_count = sum(
        1 for p in item.bronze_binary_paths
        if pipeline.FORMAT_BY_EXTENSION.get(os.path.splitext(p)[1].lower()) == "image"
    )
    return image_count > 1


def _workflow_marker(*, fin_path, bucket, gcs_subpath, workflow_name, params={}):
    """Builds a workflow-completion marker matching a fin `workflow` Postgres
    row's shape (fin/services/fin/node-utils/lib/workflow/postgres.js's
    `workflow` table columns: workflow_id, created, updated, type, name,
    state, data, error), for fin's workflowExistsInGcs()/loadWorkflowFromGcs()
    to pick up and treat as an already-completed Cloud Workflow run. See this
    module's docstring for the execution-testing caveat.

    workflow_id is deterministic (uuid5 of fin_path+workflow_name) rather
    than random, so re-running a sync for the same item/workflow overwrites
    the same marker instead of accumulating look-alike records. Must be a
    bare UUID string, no prefix -- fin's `workflow` Postgres table has
    workflow_id typed as a real `uuid` column (postgres.js's reloadWorkflow()
    passes it straight through in a parameterized INSERT/UPDATE), and
    Postgres rejects anything that isn't valid UUID syntax. Confirmed via a
    real execution test: a prefixed id (an earlier version of this function
    used "argonath-sync-{uuid}") caused a 500 ("invalid input syntax for
    type uuid") when fin's dbsync tried to auto-trigger the workflow, which
    surfaces as clientMedia never getting populated (image/PDF viewer shows
    "No clientMedia" in the browser).

    :param str fin_path: This binary's fcrepo path.
    :param str bucket: Target GCS bucket name.
    :param str gcs_subpath: 'images' or 'streaming'.
    :param str workflow_name: One of WORKFLOW_NAME's values.
    :param dict params: Extra params to embed in the marker's data, mirroring
        what a real Cloud Workflow execution would have recorded.
    :returns: Marker dict, ready to json.dumps() and upload to
        'workflows{fin_path}/{workflow_name}.json'.
    :rtype: dict
    """
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    workflow_id = str(uuid.uuid5(uuid.NAMESPACE_URL, fin_path + workflow_name))
    return {
        "workflow_id": workflow_id,
        "id": workflow_id,
        "name": workflow_name,
        "type": "gc-workflow",
        "state": "completed",
        "created": now,
        "updated": now,
        "error": None,
        "data": {
            "finPath": fin_path,
            "gcsBucket": bucket,
            # workflow-model.js's _updateWorkflowBuckets() -- called
            # unconditionally by getWorkflowInfo(), which transform.js's
            # clientMedia-population code calls to check "has this workflow
            # completed" -- unconditionally does
            # `workflow.data.tmpGcsPath.replace(...)` with no null-check.
            # A real Cloud Workflow marker always has this from creation
            # (createWorkflow() sets it to 'gs://{tmpGcsBucket}/{workflowId}/
            # {filename}'); without it here, every clientMedia fetch 500s,
            # which dbsync treats as "resource inaccessible" and deletes the
            # binary from the ES index entirely -- worse than a missing
            # clientMedia field, the whole node vanishes. The exact bucket
            # name is irrelevant here (no real tmp upload ever happened for
            # a synthetic already-completed marker) -- it only needs to be a
            # non-empty string so .replace() doesn't crash. Confirmed via a
            # real fin import (2026-08-26) reproducing this exact TypeError.
            "tmpGcsBucket": bucket,
            "tmpGcsPath": f"gs://{bucket}/{workflow_id}/{fin_path.rsplit('/', 1)[-1]}",
            "gcsSubpath": gcs_subpath,
            # Without this, workflow-model.js's notifyOnSuccess() silently
            # no-ops (`if (!workflow.data.notifyOnSuccess) return;`), so fin
            # never re-indexes the item after loading this marker -- the ES
            # document it already built (with clientMedia still empty, since
            # indexing always runs before the workflow auto-trigger in
            # dams/services/fin/models/item/model.js's update()) stays stale
            # forever. Value matches the real default in
            # dams/services/init/fcrepo/fin/workflows/config.json
            # (defaults.notifyOnSuccess), confirmed via a real fin import
            # (2026-08-25) where clientMedia stayed empty on every import --
            # including a from-scratch one with GCS already populated
            # beforehand -- until this field was added.
            "notifyOnSuccess": "/svc:reindex?no-crawl=true",
            "params": json.dumps(params),
        },
    }


def _sync_outputs(item, *, rel_dir, fin_path, gcs_subpath, bucket, page=None, ocr_rel_dir=None, stem=None, multi=False):
    """Reads a published silver process directory's manifest.json and every
    file listed in its 'outputs', and copies them into GCS at the equivalent
    fin path. The generic half of every media type's sync -- translating
    manifest.json's own content into the DAMS shape is media-type-specific
    and handled separately by callers that need it (image/PDF only; video
    has no DAMS-shaped manifest to produce, see module docstring).

    :param digtk.lib.items.ItemLocation item:
    :param str rel_dir: CaskFS process dir, relative to the item root (e.g.
        'public-frontend-derivatives/images/photo.tif/images').
    :param str fin_path: This binary's fcrepo path -- see _fin_path(). Built
        by the caller (not here), since whether it needs a 'media' segment
        depends on the whole item's binary count, not just this one dir.
    :param str gcs_subpath: 'images' or 'streaming' -- see GCS_SUBPATH.
    :param str bucket: Target GCS bucket.
    :param int|None page: 1-indexed PDF page number, or None for non-paginated media.
    :param str|None ocr_rel_dir: CaskFS process dir the real 'ocr.djvu'/
        'ocr-stats.json' files live in (see
        digtk.models.digital_website.pipeline.build_ocr_products()) -- a
        *different* directory than rel_dir, since OCR products are published
        separately from the small/medium/large/tiled set (and, for a
        multi-source item, shared across every source rather than nested
        per-source). None (the default) skips OCR entirely -- e.g. for an
        item processed before this feature existed. Missing djvu/stats files
        in an otherwise-published ocr dir (same reason) are tolerated the
        same way, not raised.
    :param str|None stem: This source's filename stem -- needed to recover
        the exact filename digtk.models.digital_website.pipeline.output_name()
        gave ocr.djvu/ocr-stats.json in a shared multi-source ocr dir. Only
        meaningful together with ocr_rel_dir.
    :param bool multi: See output_name(); only meaningful together with ocr_rel_dir.
    :returns: {"manifest": parsed argonath manifest.json (with a real "ocr"
        entry merged in if ocr_rel_dir was given and had one),
        "gcs_prefix": str (bucket-relative dir uploaded into)}
    :rtype: dict
    """
    cask_dir = pipeline.derivative_dir(item, rel_dir)
    manifest = json.loads(fs.read(f"{cask_dir}/manifest.json"))

    page_segment = f"/{page}" if page is not None else ""
    # fin_path already starts with "/item/..." (see _fin_path) -- getGcsBasePath()'s
    # formula is bucket + finPath + '/' + gcsSubpath, i.e. bucket-relative key =
    # finPath (leading slash stripped) + '/' + gcsSubpath. No separate "item/"
    # prefix gets prepended here -- it's already the first segment of fin_path.
    gcs_prefix = f"{fin_path.lstrip('/')}/{gcs_subpath}{page_segment}"

    for output_filename in manifest.get("outputs", []):
        data = fs.read(f"{cask_dir}/{output_filename}")
        _upload(bucket, f"{gcs_prefix}/{output_filename}", data)

    if ocr_rel_dir is not None:
        _sync_ocr_outputs(item, ocr_rel_dir=ocr_rel_dir, stem=stem, multi=multi, gcs_prefix=gcs_prefix, bucket=bucket)

    return {"manifest": manifest, "gcs_prefix": gcs_prefix}


def _sync_ocr_outputs(item, *, ocr_rel_dir, stem, multi, gcs_prefix, bucket):
    """Uploads this source's real 'ocr.djvu'/'ocr-stats.json' (if present)
    from their own, separately-published CaskFS ocr/ directory into the same
    GCS prefix _sync_outputs() is uploading the rest of this source's
    products into -- under their plain, un-prefixed filenames (matching
    production's layout exactly), even though a shared multi-source ocr dir
    names them with an output_name() stem prefix on disk.

    Tries reading each expected file directly rather than first checking the
    ocr dir's own manifest.json's "outputs" list. Items processed before
    pdf.py namespaced a mixed item's PDF ocr/ output (see
    pipeline.pdf_ocr_prefix()) can still have a bare ocr/manifest.json whose
    "outputs" list was overwritten by whichever of image/PDF derivatives ran
    last; reading the files directly works either way.

    :param digtk.lib.items.ItemLocation item:
    :param str ocr_rel_dir: See _sync_outputs().
    :param str|None stem: See _sync_outputs().
    :param bool multi: See _sync_outputs().
    :param str gcs_prefix: Bucket-relative dir to upload into (same one
        _sync_outputs() used for this source's other products).
    :param str bucket: Target GCS bucket.
    """
    ocr_cask_dir = pipeline.derivative_dir(item, ocr_rel_dir)
    for plain_filename in ("ocr.djvu", "ocr-stats.json"):
        published_filename = pipeline.output_name(stem, plain_filename, multi)
        try:
            data = fs.read(f"{ocr_cask_dir}/{published_filename}")
        except FileNotFoundError:
            continue
        _upload(bucket, f"{gcs_prefix}/{plain_filename}", data)


def _dams_image_manifest(argonath_manifest, *, fin_path, gcs_subpath, page=None):
    """Translates an argonath image/PDF-page manifest.json (plain cask://
    URLs) into the shape
    dams/services/image-utils/lib/image-manifest-to-dams-manifest.js
    produces -- {{BUCKET}}-templated fcrepo/svc:gcs URLs, matching what
    dams/services/fin/models/item/transform.js reads back out at render
    time. The '{{BUCKET}}' placeholder in 'url' is substituted with the
    actual requesting bucket by fin's gcs service at read time
    (fin/services/fin/gcs/index.js) -- it is not resolved here.

    Builds an explicit clean dict rather than copying argonath_manifest
    through -- confirmed against two real production clientMedia responses
    (2026-08-26, ark:/87293/d3gq6r65h -- once against a single page, once
    against the full 128-page pdf.pages array) that the legacy shape is
    exactly {original?, small, medium, large, tiled(+iiif), page?,
    extractDensity?, ocr?}, and that each product itself is exactly {size,
    url} (tiled adds iiif) -- no other per-product field, in particular no
    'gcsPath'. An earlier version of this function (a) copied
    argonath_manifest's own bookkeeping fields through (written_at, params,
    command, status, duration_s), and (b) added a 'gcsPath' per product
    (reverse-engineered from workflow-service-utils.js's getGcsBasePath()
    formula, assumed to be part of the client-facing shape but never
    execution-verified) -- neither survives in a real production response.

    'ocr' passes through argonath_manifest's own real "ocr" entry (see
    digtk.models.digital_website.pipeline's build_ocr_products(), which
    replicates legacy image-utils' djvu-text-layer generation) whenever
    present. BookReaderModel.js's _isValidPage() requires ocr.url/ocr.size
    on a page or it's rejected outright, crashing the whole book reader --
    an item without a real "ocr" entry (not yet reprocessed with this
    feature) gets no 'ocr' field synced at all and hits that same crash;
    there's no stub fallback. Critically, for a PDF+ImageList item the book
    reader's page list is NOT built from the PDF's own aggregate manifest at
    all -- client-media/model.js's handleImageList()
    (client/public/lib/utils/client-media/model.js:282-323) builds
    node.clientMedia.pages synchronously, directly from each individual
    per-page TIFF binary's own node.clientMedia.images (i.e. this function's
    output when called from sync_image_item(), not sync_pdf_item()) -- so
    'ocr' must be populated for both call sites, not just PDF pages.
    Everything else here is a straightforward reproduction of what a real
    completed image-utils run outputs.

    :param dict argonath_manifest: Parsed silver manifest.json (image.py's
        generate()/pdf.py's derivatives() 'extra' shape -- original/small/
        medium/large/tiled/ocr {size,url} entries merged onto the generic
        written_at/status/command/params/outputs manifest fields).
    :param str fin_path: This binary's fcrepo path -- see _fin_path().
    :param str gcs_subpath: 'images' (only image/pdf call this).
    :param int|None page: 0-indexed PDF page number -- both the storage path
        segment *and* the 'page' display field are 0-indexed in real
        clientMedia responses (confirmed: prod's first PDF page reads
        "page": 0, not 1) -- or None for a plain image (no page segment,
        no 'page' field).
    :returns: DAMS-shaped manifest dict.
    :rtype: dict
    """
    page_segment = f"/{page}" if page is not None else ""
    dams_manifest = {}

    if page is not None:
        dams_manifest["page"] = page

    if "original" in argonath_manifest:
        dams_manifest["original"] = {
            "size": argonath_manifest["original"]["size"],
            "url": f"/fcrepo/rest{fin_path}",
        }

    for key, product_filename in _IMAGE_PRODUCT_FILENAME.items():
        product = argonath_manifest.get(key)
        if not product:
            continue
        dams_manifest[key] = {
            "size": product["size"],
            "url": f"/fcrepo/rest{fin_path}/svc:gcs/{{{{BUCKET}}}}/{gcs_subpath}{page_segment}/{product_filename}",
        }

    if "tiled" in dams_manifest:
        dams_manifest["tiled"]["iiif"] = f"/fcrepo/rest{fin_path}/svc:iiif/{gcs_subpath}{page_segment}/tiled.tif"

    if "ocr" in argonath_manifest:
        # Real content now (see digtk.models.digital_website.pipeline's
        # build_ocr_products() and _sync_ocr_outputs() above, which actually
        # uploads the ocr.djvu/ocr-stats.json files this URL points at) --
        # an earlier version of this field was a stub {size, imageSize, url}
        # shaped correctly but pointing at a URL that always 404'd, added
        # purely to satisfy BookReaderModel.js's _isValidPage() (requires
        # ocr.url/ocr.size on every page or the whole book reader crashes --
        # see module docstring). An item that hasn't been reprocessed with
        # the real OCR feature yet has no "ocr" key here at all and gets no
        # 'ocr' field synced -- that reintroduces the same book-reader crash
        # for it specifically, until it's reprocessed; there's no stub
        # fallback anymore. Applies to plain images too (not just PDF
        # pages): handleImageList() reads each individual per-page TIFF's
        # own clientMedia.images -- i.e. sync_image_item()'s output -- to
        # build the book reader's page list, never the PDF's own aggregate
        # manifest.
        dams_manifest["ocr"] = {
            "size": argonath_manifest["ocr"]["size"],
            "imageSize": argonath_manifest["ocr"]["imageSize"],
            "url": f"/fcrepo/rest{fin_path}/svc:gcs/{{{{BUCKET}}}}/{gcs_subpath}{page_segment}/ocr.djvu",
        }

    if "extractDensity" in argonath_manifest:
        dams_manifest["extractDensity"] = argonath_manifest["extractDensity"]

    return dams_manifest


def _pdf_page_numbers(images_base_dir):
    """Lists the 1-indexed page subdirectories under a PDF's images/ process dir.

    :param str images_base_dir: CaskFS path (cask:/...) to the PDF's images/ dir.
    :returns: Page numbers, ascending.
    :rtype: list[int]
    """
    entries = fs.ls(images_base_dir, opts={"limit": 10000})
    return sorted(int(e.name) for e in entries if e.kind == "directory" and e.name.isdigit())


def _page_tag(page_index, count):
    """Zero-padded page directory name -- mirrors
    digtk.models.digital_website.pdf._page_tag() exactly (kept as an
    independent copy here rather than importing a private helper across
    modules), since it's what pdf.py actually named each page's ocr/
    subdirectory when publishing.

    :param int page_index: 0-indexed page number.
    :param int count: Total page count (pad width scales with this, minimum 3 digits).
    :rtype: str
    """
    pad = max(3, len(str(max(count - 1, 0))))
    return f"page-{page_index:0{pad}d}"


def sync_image_item(ark, opts={}):
    """Syncs every image bronze binary's already-published derivative set
    into GCS, DAMS-manifest-shaped, plus a workflow-completion marker so
    fin's import skips re-generating them. See this module's docstring for
    the overall approach and its caveats.

    :param str ark: Item ARK.
    :param dict opts: 'bucket' (str, default CONFIG["gcs"]["bucket"]).
    :returns: {"ark": str, "bucket": str, "images": {filename: {"finPath": str, "gcsPrefix": str}}}
    :rtype: dict
    """
    bucket = opts.get("bucket") or CONFIG["gcs"]["bucket"]
    item = items.resolve(ark)
    image_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "image")
    is_media = _needs_media_wrapper(item)
    # Matches image.py's generate()'s own 'multi' -- the ocr/ dir is shared
    # across every image source in the item, so output_name() only
    # disambiguates ocr.djvu/ocr-stats.json's filenames when there's more
    # than one (distinct from is_media, which is about fin-path wrapping).
    multi = len(image_paths) > 1

    results = {}
    for bronze_path in image_paths:
        filename = bronze_path.rsplit("/", 1)[-1]
        stem = os.path.splitext(filename)[0]
        fin_path = _fin_path(ark, filename, media=is_media, subcontainer="images")
        rel_dir = f"{pipeline.PUBLIC_FRONTEND_DIR}/images/{filename}/images"
        synced = _sync_outputs(
            item, rel_dir=rel_dir, fin_path=fin_path,
            gcs_subpath=GCS_SUBPATH["image"], bucket=bucket,
            ocr_rel_dir="ocr", stem=stem, multi=multi,
        )

        dams_manifest = _dams_image_manifest(
            synced["manifest"], fin_path=fin_path, gcs_subpath=GCS_SUBPATH["image"],
        )
        _upload(bucket, f"{synced['gcs_prefix']}/manifest.json", json.dumps(dams_manifest, indent=2).encode())

        marker = _workflow_marker(
            fin_path=fin_path, bucket=bucket, gcs_subpath=GCS_SUBPATH["image"],
            workflow_name=WORKFLOW_NAME["image"],
        )
        _upload(bucket, f"workflows{fin_path}/{WORKFLOW_NAME['image']}.json", json.dumps(marker, indent=2).encode())

        results[filename] = {"finPath": fin_path, "gcsPrefix": synced["gcs_prefix"]}

    return {"ark": ark, "bucket": bucket, "images": results}


def sync_pdf_item(ark, opts={}):
    """Syncs every PDF bronze binary's already-published per-page derivative
    set into GCS, DAMS-manifest-shaped (both per-page and the aggregate
    images/manifest.json legacy DAMS expects, mirroring
    dams/services/image-utils/lib/workflow-service-utils.js's
    finalizePdfWorkflow()), plus a workflow-completion marker.

    Page numbers are 0-indexed throughout -- both the GCS storage directory
    and the 'page' field inside each page's manifest content -- not
    argonath's own 1-indexed page numbers (argonath's page directories in
    CaskFS, e.g. '.../images/1/', stay 1-indexed; only the GCS/manifest
    output changes). Two things independently confirmed this against a real
    fin instance (2026-08-26): dams/services/fin/models/item/transform.js
    hardcodes a fetch to the PDF's finPath + '/images/0/manifest.json' for a
    general clientMedia preview, so the first page must live at
    '.../images/0/...' or clientMedia.images 404s; and a real production
    clientMedia response for this same item (ark:/87293/d3gq6r65h) shows its
    first page's own 'page' field reads 0, not 1, confirming the display
    value is 0-indexed too, not just the storage path.

    :param str ark: Item ARK.
    :param dict opts: 'bucket' (str, default CONFIG["gcs"]["bucket"]).
    :returns: {"ark": str, "bucket": str, "pdfs": {filename: {"finPath": str, "pages": int}}}
    :rtype: dict
    """
    bucket = opts.get("bucket") or CONFIG["gcs"]["bucket"]
    item = items.resolve(ark)
    pdf_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "pdf")
    is_media = _needs_media_wrapper(item)

    results = {}
    for bronze_path in pdf_paths:
        filename = bronze_path.rsplit("/", 1)[-1]
        # The same rule pdf.py's derivatives() writes with -- a multi-PDF *or*
        # mixed-format item nests each PDF's ocr/ under its own stem.
        ocr_prefix = pipeline.pdf_ocr_prefix(item.bronze_binary_paths, bronze_path)
        images_base = f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/images"
        fin_path = _fin_path(ark, filename, media=is_media)
        page_numbers = _pdf_page_numbers(pipeline.derivative_dir(item, images_base))
        page_count = len(page_numbers)

        dams_pages = []
        for zero_indexed_page, page in enumerate(page_numbers):
            page_tag = _page_tag(zero_indexed_page, page_count)
            synced = _sync_outputs(
                item, rel_dir=f"{images_base}/{page}", fin_path=fin_path,
                gcs_subpath=GCS_SUBPATH["pdf"], bucket=bucket, page=zero_indexed_page,
                # Each page already publishes into its own page_tag-unique
                # ocr/ subdir (see pdf.py's derivatives()), so ocr.djvu/
                # ocr-stats.json are never output_name()-prefixed there --
                # multi=False always here, matching how they were written.
                ocr_rel_dir=f"{ocr_prefix}ocr/{page_tag}", stem=None, multi=False,
            )
            dams_page_manifest = _dams_image_manifest(
                synced["manifest"], fin_path=fin_path, gcs_subpath=GCS_SUBPATH["pdf"],
                page=zero_indexed_page,
            )
            _upload(bucket, f"{synced['gcs_prefix']}/manifest.json", json.dumps(dams_page_manifest, indent=2).encode())
            dams_pages.append(dams_page_manifest)

        dams_pages.sort(key=lambda p: p["page"])
        aggregate_gcs_prefix = f"{fin_path.lstrip('/')}/{GCS_SUBPATH['pdf']}"
        _upload(bucket, f"{aggregate_gcs_prefix}/manifest.json", json.dumps({"pages": dams_pages}, indent=2).encode())

        marker = _workflow_marker(
            fin_path=fin_path, bucket=bucket, gcs_subpath=GCS_SUBPATH["pdf"],
            workflow_name=WORKFLOW_NAME["pdf"], params={"pages": len(page_numbers)},
        )
        _upload(bucket, f"workflows{fin_path}/{WORKFLOW_NAME['pdf']}.json", json.dumps(marker, indent=2).encode())

        results[filename] = {"finPath": fin_path, "pages": len(page_numbers)}

    return {"ark": ark, "bucket": bucket, "pdfs": results}


def sync_video_item(ark, opts={}):
    """Syncs every video bronze binary's already-published HLS stream files
    into GCS, plus a workflow-completion marker. No DAMS-shaped manifest.json
    is produced -- dams/services/fin/models/item/transform.js references the
    synced playlist.m3u8 directly, it never reads a manifest for video --
    confirmed against a real production clientMedia response (2026-08-27,
    ark:/87293/d3361t): clientMedia.streamingVideo.manifest is just a
    literal '.../streaming/playlist.m3u8' URL, no manifest.json object
    exists anywhere in the real GCS bucket for a video.

    A video's fin path never gets the 'media' wrapper, regardless of how
    many other bronze binaries the item has (a poster image, a transcript
    PDF, captions) -- see _fin_path()'s docstring; confirmed against every
    real video example sampled, 0 exceptions.

    :param str ark: Item ARK.
    :param dict opts: 'bucket' (str, default CONFIG["gcs"]["bucket"]).
    :returns: {"ark": str, "bucket": str, "videos": {filename: {"finPath": str, "gcsPrefix": str}}}
    :rtype: dict
    """
    bucket = opts.get("bucket") or CONFIG["gcs"]["bucket"]
    item = items.resolve(ark)
    video_paths = pipeline.filter_binaries_by_format(item.bronze_binary_paths, "video")

    results = {}
    for bronze_path in video_paths:
        filename = bronze_path.rsplit("/", 1)[-1]
        fin_path = _fin_path(ark, filename)
        rel_dir = f"{pipeline.PUBLIC_FRONTEND_DIR}/{filename}/streaming"
        synced = _sync_outputs(
            item, rel_dir=rel_dir, fin_path=fin_path,
            gcs_subpath=GCS_SUBPATH["video"], bucket=bucket,
        )

        marker = _workflow_marker(
            fin_path=fin_path, bucket=bucket, gcs_subpath=GCS_SUBPATH["video"],
            workflow_name=WORKFLOW_NAME["video"],
        )
        _upload(bucket, f"workflows{fin_path}/{WORKFLOW_NAME['video']}.json", json.dumps(marker, indent=2).encode())

        results[filename] = {"finPath": fin_path, "gcsPrefix": synced["gcs_prefix"]}

    return {"ark": ark, "bucket": bucket, "videos": results}


_SYNC_FN_BY_FORMAT = {
    "image": ("images", sync_image_item),
    "pdf": ("pdfs", sync_pdf_item),
    "video": ("videos", sync_video_item),
}


def sync_item(ark, opts={}):
    """Syncs every derivative already published for an item into GCS, across
    every media type actually present on it -- images, PDFs, and videos.
    Audio is never synced (see module docstring). Skips a media type
    entirely (rather than erroring) when the item simply has none of it; an
    item with a genuinely unrecognized bronze binary extension still raises,
    matching filter_binaries_by_format()'s existing behavior for every other
    digtk command.

    An image or PDF binary's fin path gets a 'media' container segment
    (/item/{ark}/media/{filename}) only when the item has more than one
    image-format bronze binary (see _needs_media_wrapper()), not simply
    "more than one bronze binary of any kind" -- a lone poster image or
    transcript PDF sitting alongside a video attaches directly at the item
    root, no wrapper, regardless of total binary count. TIFF images
    additionally nest under their own 'images' subcontainer inside 'media'
    when wrapped (a compiled PDF does not). A video's fin path never gets
    the 'media' wrapper at all, unconditionally -- see _fin_path()'s and
    sync_video_item()'s docstrings. This is a structural inference from
    fin-api's binary-attachment model, not a hard rule enforced anywhere in
    code (fin-api mirrors whatever directory layout a collection's source
    metadata git repo happens to use) -- if a given collection's real import
    doesn't follow this pattern, the synced path won't match and the "skip
    regeneration" bootstrap (see module docstring) won't work for it. Getting
    this wrong is silent at sync time (upload succeeds either way) but fails
    at import time -- fin's clientMedia lookup 404s against the wrong path,
    which cascades into dbsync auto-triggering regeneration anyway, so a
    "successful" sync isn't proof the path shape is right; only a real import
    against a real fin instance confirms it (as it did for image vs PDF here).

    A multi-format item (see docs/cask-conventions.md's "Multi-Format Items"
    -- e.g. TIFF page masters alongside a compiled PDF, sharing one ark) may
    have bronze binaries of a format whose derivatives simply haven't been
    generated yet (`digtk showcase image derivatives` run, but not `digtk
    showcase pdf derivatives`, or vice versa) -- that format's silver process directory
    won't exist in CaskFS yet. That's reported back in 'skipped', not raised,
    since it's an ordinary "not generated yet" state, not a real failure --
    the caller decides whether to run the missing derivatives step and
    re-sync, or proceed with a partial sync.

    :param str ark: Item ARK.
    :param dict opts: 'bucket' (str, default CONFIG["gcs"]["bucket"]).
    :returns: {"status": "success", "ark": str, "bucket": str, "images": {...},
        "pdfs": {...}, "videos": {...}, "skipped": {media_type: reason}} --
        only the keys for media types actually present on the item are
        included; 'skipped' is present only if at least one present media
        type had no derivatives to sync yet.
    :rtype: dict
    """
    bucket = opts.get("bucket") or CONFIG["gcs"]["bucket"]
    item = items.resolve(ark)

    formats_present = {
        pipeline.FORMAT_BY_EXTENSION.get(os.path.splitext(p)[1].lower())
        for p in item.bronze_binary_paths
    }

    result = {"status": "success", "ark": ark, "bucket": bucket}
    skipped = {}
    for fmt, (key, sync_fn) in _SYNC_FN_BY_FORMAT.items():
        if fmt not in formats_present:
            continue
        try:
            result[key] = sync_fn(ark, opts)[key]
        except FileNotFoundError:
            skipped[fmt] = f"no {fmt} derivatives published yet for this item (run `digtk showcase {fmt} derivatives {ark}` first)"

    if skipped:
        result["skipped"] = skipped

    return result
