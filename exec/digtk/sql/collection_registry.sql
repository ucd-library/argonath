-- collection_registry schema — durable processing-state store for Dagster/CaskFS
-- pipeline work: which collections/items argonath is tracking (including
-- each collection's source registration: source_type, workflow tags, status),
-- their external identifiers, a work queue sensors poll/claim from, and
-- per-job results (consumed by sensors deciding what needs to run, collection
-- partition creation in Dagster, and general reporting in Superset).
--
-- Visibility is not tracked here — dc-showcase's dcf_edits.collection_edit
-- owns it.
--
-- A job_queue row is a claim ticket: a sensor deletes the row when it picks
-- up the work, and from that point the in-flight state lives in Dagster's
-- own run tracking until a job_result row is written back (via
-- upsert_job_result below) — there is no "status" column here by design.

CREATE SCHEMA IF NOT EXISTS collection_registry;

-- ---------------------------------------------------------------------------
-- collection / item
-- ---------------------------------------------------------------------------

-- name is the collection key, "bronze/[group]/[slug]". source_type 'unknown'
-- marks a collection collection_registry saw (e.g. a hand-run `digtk state`
-- command) but nothing registered; registration lookups skip those rows.
CREATE TABLE IF NOT EXISTS collection_registry.collection (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL UNIQUE,
    group_name       TEXT GENERATED ALWAYS AS (split_part(name, '/', 2)) STORED,
    slug             TEXT GENERATED ALWAYS AS (split_part(name, '/', 3)) STORED,
    source_type      TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (source_type IN ('manual', 'preservica', 'unknown')),
    workflow_tags    TEXT[] NOT NULL DEFAULT '{}',  -- from the 'argonath' identifier, e.g. {sync,ai}
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
    last_synced_at   TIMESTAMPTZ,
    last_updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collection_registry.item (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name             TEXT NOT NULL UNIQUE,
    last_updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- identifiers — multiple external identifiers per collection/item
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_registry.collection_identifiers (
    id            SERIAL PRIMARY KEY,
    collection_id UUID NOT NULL REFERENCES collection_registry.collection(id) ON DELETE CASCADE,
    id_type       TEXT NOT NULL,
    id_value      TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, id_type, id_value)
);

CREATE INDEX IF NOT EXISTS collection_identifiers_lookup_idx
    ON collection_registry.collection_identifiers (id_type, id_value);

-- A slug must be unique among active registered collections; a disabled one
-- frees its slug. No auto-disambiguation — see
-- digtk.models.collection.register_collection().
CREATE UNIQUE INDEX IF NOT EXISTS collection_active_slug_uq
    ON collection_registry.collection (slug)
    WHERE status = 'active' AND source_type <> 'unknown';

CREATE TABLE IF NOT EXISTS collection_registry.item_identifiers (
    id         SERIAL PRIMARY KEY,
    item_id    UUID NOT NULL REFERENCES collection_registry.item(id) ON DELETE CASCADE,
    id_type    TEXT NOT NULL,
    id_value   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (item_id, id_type, id_value)
);

CREATE INDEX IF NOT EXISTS item_identifiers_lookup_idx
    ON collection_registry.item_identifiers (id_type, id_value);

-- ---------------------------------------------------------------------------
-- collection_item — relates table; an item can belong to multiple
-- collections. Cascades on delete of either side.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_registry.collection_item (
    id            SERIAL PRIMARY KEY,
    collection_id UUID NOT NULL REFERENCES collection_registry.collection(id) ON DELETE CASCADE,
    item_id       UUID NOT NULL REFERENCES collection_registry.item(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, item_id)
);

CREATE INDEX IF NOT EXISTS collection_item_item_idx
    ON collection_registry.collection_item (item_id);

-- ---------------------------------------------------------------------------
-- job_queue — pending work. A sensor claims a row by deleting it; no status
-- column (see header comment).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_registry.job_queue (
    id            SERIAL PRIMARY KEY,
    collection_id UUID REFERENCES collection_registry.collection(id) ON DELETE CASCADE,
    item_id       UUID REFERENCES collection_registry.item(id) ON DELETE CASCADE,
    job_name      TEXT NOT NULL,
    config        JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(collection_id, item_id) = 1)
);

CREATE INDEX IF NOT EXISTS job_queue_poll_idx
    ON collection_registry.job_queue (job_name, collection_id, item_id);

-- ---------------------------------------------------------------------------
-- job_result — exactly one row per (job_name, collection) or
-- (job_name, item), kept current via upsert_job_result(). Read by sensors
-- deciding what needs to run, Dagster partition creation, and Superset.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_registry.job_result (
    id            SERIAL PRIMARY KEY,
    collection_id UUID REFERENCES collection_registry.collection(id) ON DELETE CASCADE,
    item_id       UUID REFERENCES collection_registry.item(id) ON DELETE CASCADE,
    job_name      TEXT NOT NULL,
    result        JSONB,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(collection_id, item_id) = 1)
);

-- Two partial unique indexes rather than one plain UNIQUE(job_name,
-- collection_id, item_id): NULLs don't conflict under a plain multi-column
-- UNIQUE constraint, so that alone wouldn't stop duplicate rows for the same
-- (job_name, collection_id) pair when item_id is NULL on both.
CREATE UNIQUE INDEX IF NOT EXISTS job_result_collection_uq
    ON collection_registry.job_result (job_name, collection_id) WHERE item_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS job_result_item_uq
    ON collection_registry.job_result (job_name, item_id) WHERE collection_id IS NULL;

-- Upsert so there is ever only one job_result row per (job_name, target).
-- Picks whichever partial unique index above applies based on which of
-- p_collection_id / p_item_id is set.
CREATE OR REPLACE FUNCTION collection_registry.upsert_job_result(
    p_job_name TEXT,
    p_collection_id UUID,
    p_item_id UUID,
    p_result JSONB
) RETURNS collection_registry.job_result AS $$
DECLARE
    v_row collection_registry.job_result;
BEGIN
    IF num_nonnulls(p_collection_id, p_item_id) != 1 THEN
        RAISE EXCEPTION 'upsert_job_result: exactly one of p_collection_id / p_item_id must be set';
    END IF;

    IF p_collection_id IS NOT NULL THEN
        INSERT INTO collection_registry.job_result (job_name, collection_id, item_id, result, updated_at)
        VALUES (p_job_name, p_collection_id, NULL, p_result, now())
        ON CONFLICT (job_name, collection_id) WHERE item_id IS NULL
        DO UPDATE SET result = EXCLUDED.result, updated_at = now()
        RETURNING * INTO v_row;
    ELSE
        INSERT INTO collection_registry.job_result (job_name, collection_id, item_id, result, updated_at)
        VALUES (p_job_name, NULL, p_item_id, p_result, now())
        ON CONFLICT (job_name, item_id) WHERE collection_id IS NULL
        DO UPDATE SET result = EXCLUDED.result, updated_at = now()
        RETURNING * INTO v_row;
    END IF;

    RETURN v_row;
END;
$$ LANGUAGE plpgsql;
