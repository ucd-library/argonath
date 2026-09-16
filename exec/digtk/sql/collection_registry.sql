-- Collection registry schema — tracks which collections argonath knows about
-- (source, name, workflow tags) so Dagster sensors know what to watch and
-- what to do when a source changes. See docs/preservica-identifiers.md and
-- digtk.models.collection (register_collection / get_registration /
-- list_active_collections / init_pg).
--
-- Applied to the shared Postgres/pgvector instance project-anduin provisions
-- (argonath itself does not provision that infrastructure) — coordinate with
-- whoever administers it, or run `digtk collection init-pg` against it. Lives
-- in its own 'argonath' schema, namespaced apart from anything else in that
-- shared instance (e.g. project-anduin's own tables, or the pgvector store
-- digtk.lib.ai's RAG indexing uses). Not Dagster's own run/event-log Postgres
-- database either (see dagster/dagster.yaml's storage.postgres block) — a
-- separate database/credentials, resolved via digtk.lib.db / CONFIG["postgres"].

CREATE SCHEMA IF NOT EXISTS argonath;

CREATE TABLE IF NOT EXISTS argonath.collection_registry (
    id             SERIAL PRIMARY KEY,
    name           TEXT NOT NULL,                -- slugified title, matches the bronze dir name
    group_name     TEXT NOT NULL,                -- bronze group segment
    source_type    TEXT NOT NULL CHECK (source_type IN ('manual', 'preservica')),
    workflow_tags  TEXT[] NOT NULL DEFAULT '{}',  -- denormalized from the 'argonath' identifier, e.g. {sync,ai,public}
    status         TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    last_synced_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Name must be unique among active collections; a disabled collection frees up
-- its name for reuse. No auto-disambiguation on collision — see
-- digtk.models.collection.register_collection().
CREATE UNIQUE INDEX IF NOT EXISTS collection_registry_active_name_uq
    ON argonath.collection_registry (name) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS argonath.collection_identifiers (
    id            SERIAL PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES argonath.collection_registry(id) ON DELETE CASCADE,
    id_type       TEXT NOT NULL,   -- 'preservica_ref', 'ark', 'LocalID', 'argonath', 'preservica_path', ...
    id_value      TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, id_type, id_value)
);

CREATE INDEX IF NOT EXISTS collection_identifiers_lookup_idx
    ON argonath.collection_identifiers (id_type, id_value);
