"""
A collection's visibility, read from dc-showcase's dcf_edits.collection_edit
table (keyed by ES @id), and the ES roles its documents get. A collection
with no row is private. Connection settings: CONFIG["showcase_db"].
"""
from digtk.models.digital_website import showcase_db

DEFAULT = "private"

# admins see private documents
ROLES = {"public": ["public", "admin"], "private": ["admin"]}


def connect():
    """Opens a read-only connection to dc-showcase's Postgres (see showcase_db.connect()).

    :returns: A live psycopg2 connection.
    :rtype: psycopg2.extensions.connection
    """
    return showcase_db.connect({"readonly": True})


def lookup(collection_ids, opts={}):
    """The visibility of each collection; one with no row (or no valid value) is private.

    :param list[str] collection_ids: Collection ES @ids.
    :param dict opts: 'conn' (injectable connection, left open).
    :returns: {collection_id: "public"|"private"}.
    :rtype: dict
    :raises psycopg2.Error: If the database can't be reached or queried.
    """
    ids = list(collection_ids)
    if not ids:
        return {}
    conn = opts.get("conn") or connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT collection_id, visibility FROM dcf_edits.collection_edit WHERE collection_id = ANY(%s)",
                (ids,),
            )
            found = dict(cur.fetchall())
    finally:
        if "conn" not in opts:
            conn.close()
    return {i: found[i] if found.get(i) in ROLES else DEFAULT for i in ids}


def roles(collection_id, opts={}):
    """The ES roles for a collection's documents. A missing collection id
    (e.g. an item with no isPartOf) is private.

    :param str|None collection_id: Collection ES @id.
    :param dict opts: 'conn' (see lookup()); 'roles_cache' (dict, filled by
        collection id so a batch looks each collection up once).
    :returns: ["public", "admin"] or ["admin"].
    :rtype: list[str]
    :raises psycopg2.Error: If the database can't be reached or queried.
    """
    if not collection_id:
        return list(ROLES[DEFAULT])
    cache = opts.get("roles_cache")
    if cache is not None and collection_id in cache:
        return list(cache[collection_id])
    value = ROLES[lookup([collection_id], opts)[collection_id]]
    if cache is not None:
        cache[collection_id] = value
    return list(value)
