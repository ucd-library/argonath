"""
digtk.lib.preservica.local_client — a drop-in PreservicaClient replacement
that reads a local directory instead of the live Preservica API.

digtk.models.collection.preservica_import() already accepts an injectable
`opts['client']` (originally added for tests) — this module is that same
extension point exposed on the CLI (`digtk preservica import --source-dir
<path>`), for staging/testing a collection offline before it exists in
Preservica, or replaying an already-fetched export without hitting the
network again. Every by-ref/tree writing, registration, and error-handling
path in collection.py is shared unchanged; this class only stands in for
network calls.

Source directory layout (one directory per entity ref):

    {source_dir}/{ref}/
        entity.json        {"title": str, "description": str|None, "type": "SO"|"IO"}
        identifiers.json    [{"type": str, "value": str}, ...] -- include an
                            ARK-shaped identifier ("value" starting "ark:/")
                            for an IO to be resolvable later by digtk.lib.items;
                            omitted or missing is a warning, not an error, same
                            as the live import path.
        metadata/
            0.xml, 1.xml, ...   raw XML fragments (Dublin Core or otherwise),
                                verbatim -- same one-file-per-fragment
                                convention preservica_import() itself writes.
        children.json       [{"ref": str, "type": "SO"|"IO"}, ...] -- SO only.
        media/
            <RepresentationType>/<filename>   -- IO only, e.g. media/Preservation/
                                page_001.tif, media/Access/page_001.jpg. The
                                immediate subdirectory name under media/ *is*
                                the representationType (no representations.json
                                needed) -- for both "Preservation" and "Access"
                                are imported and treated identically downstream
                                (see dagster/lib/pipeline_fanout.py and
                                digital_website.pipeline.partition_by_format()).

This is deliberately the same shape by-ref/ itself ends up in (collection.py's
own module docstring: by-ref/ is "a near-verbatim relocation of Preservica's
own Entity API responses") minus the raw-API wrapper noise (StructuralObject/
InformationObject envelopes, metadataFragmentRefs URLs) that only exists to
round-trip through the real API -- so a directory copied out of an existing
by-ref/ tree (its entity.json/identifiers.json/children.json/metadata/ as-is,
media/ re-nested one level under a RepresentationType folder) is already a
valid source directory here.
"""
import json
import os


class LocalPreservicaClient:
    """Reads a local directory laid out like Preservica's own Entity API
    responses instead of making network calls -- see this module's docstring
    for the expected layout.

    Implements only the PreservicaClient methods digtk.models.collection
    actually calls during an import walk (get_entity, list_metadata_fragments,
    get_metadata_fragment, get_identifiers, get_children, resolve_all_content,
    download_file, get_thumbnail) -- not the full live-API surface (auth,
    pagination, get_entities_updated_since, ...), since collection.py's sync/
    changes paths aren't meaningful without a real Preservica instance to poll.

    :param str source_dir: Local directory to read from.
    """

    def __init__(self, source_dir):
        self.source_dir = source_dir

    def _entity_dir(self, ref):
        return os.path.join(self.source_dir, ref)

    def _read_json(self, ref, name, default=None):
        path = os.path.join(self._entity_dir(ref), name)
        if not os.path.exists(path):
            return default
        with open(path) as f:
            return json.load(f)

    def get_entity(self, data_type, ref, opts={}):
        """:returns: A raw-response-shaped dict wrapping this ref's entity.json,
        tagged with '_local_ref' so list_metadata_fragments() (which the real
        API's response shape wouldn't need to carry) knows which ref's
        metadata/ directory to list.
        """
        entity = self._read_json(ref, "entity.json", {})
        key = "InformationObject" if data_type == "information-objects" else "StructuralObject"
        return {
            key: {"Title": entity.get("title"), "Description": entity.get("description")},
            "_local_ref": ref,
        }

    def list_metadata_fragments(self, entity_detail):
        """:returns: [{'url': <local metadata fragment path>}, ...], sorted --
        'url' is a plain local path here, not an HTTP URL; get_metadata_fragment()
        below reads it back directly.
        """
        ref = entity_detail.get("_local_ref")
        metadata_dir = os.path.join(self._entity_dir(ref), "metadata")
        if not os.path.isdir(metadata_dir):
            return []
        return [
            {"url": os.path.join(metadata_dir, name)}
            for name in sorted(os.listdir(metadata_dir))
        ]

    def get_metadata_fragment(self, fragment_url):
        """:param str fragment_url: A local path, as returned by list_metadata_fragments().
        :returns: The fragment's raw XML text.
        :rtype: str
        """
        with open(fragment_url, encoding="utf-8") as f:
            return f.read()

    def get_identifiers(self, data_type, ref):
        """:returns: [{'type', 'value'}, ...] from this ref's identifiers.json, or []."""
        return self._read_json(ref, "identifiers.json", [])

    def get_children(self, ref, opts={}):
        """:returns: [{'ref', 'type', ...}, ...] from this SO's children.json, or []."""
        return self._read_json(ref, "children.json", [])

    def resolve_all_content(self, io_ref, entity_detail=None):
        """Derive this item's representations directly from its media/
        directory -- one representationType per immediate subdirectory, one
        entry per file in it. No fixity (nothing to compare a checksum
        against locally), so --skip-unchanged has no effect on a local import;
        every file is (re)written every run.

        :returns: [{'representationType', 'filename', 'downloadUrl', 'fixity': None}, ...].
        :rtype: list[dict]
        """
        media_dir = os.path.join(self._entity_dir(io_ref), "media")
        if not os.path.isdir(media_dir):
            return []
        representations = []
        for rep_type in sorted(os.listdir(media_dir)):
            rep_dir = os.path.join(media_dir, rep_type)
            if not os.path.isdir(rep_dir):
                continue
            for filename in sorted(os.listdir(rep_dir)):
                representations.append({
                    "representationType": rep_type,
                    "filename": filename,
                    "downloadUrl": os.path.join(rep_dir, filename),
                    "fixity": None,
                })
        return representations

    def download_file(self, download_url):
        """:param str download_url: A local path, as returned by resolve_all_content().
        :returns: The file's raw bytes.
        :rtype: bytes
        """
        with open(download_url, "rb") as f:
            return f.read()

    def get_thumbnail(self, ref):
        """No thumbnail support for a local import -- always None, which
        _walk_preservica_item() already treats as "no thumbnail" gracefully.
        """
        return None
