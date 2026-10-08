-- collection_state schema — durable processing-state store for Dagster/CaskFS
-- pipeline work: which collections/items argonath is tracking, their external
-- identifiers, a work queue sensors poll/claim from, and per-job results
-- (consumed by sensors deciding what needs to run, collection partition
-- creation in Dagster, and general reporting in Superset).
--
-- Deliberately separate from argonath.collection_registry (bronze source
-- registration — see exec/digtk/sql/collection_registry.sql) and the
-- `reporting` schema (Superset ETL command logging — see
-- exec/digtk/lib/reporting.py): both were early iterations on adjacent
-- problems; this schema is the current design and has no FK/cross-schema
-- dependency on either.
--
-- A job_queue row is a claim ticket: a sensor deletes the row when it picks
-- up the work, and from that point the in-flight state lives in Dagster's
-- own run tracking until a job_result row is written back (via
-- upsert_job_result below) — there is no "status" column here by design.

CREATE SCHEMA IF NOT EXISTS collection_state;

-- ---------------------------------------------------------------------------
-- collection / item
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_state.collection (
    id               SERIAL PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    visibility       TEXT NOT NULL CHECK (visibility IN ('public', 'private')),
    last_updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS collection_state.item (
    id               SERIAL PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    visibility       TEXT NOT NULL CHECK (visibility IN ('public', 'private')),
    last_updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- identifiers — multiple external identifiers per collection/item
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_state.collection_identifiers (
    id            SERIAL PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES collection_state.collection(id) ON DELETE CASCADE,
    id_type       TEXT NOT NULL,
    id_value      TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, id_type, id_value)
);

CREATE INDEX IF NOT EXISTS collection_identifiers_lookup_idx
    ON collection_state.collection_identifiers (id_type, id_value);

CREATE TABLE IF NOT EXISTS collection_state.item_identifiers (
    id         SERIAL PRIMARY KEY,
    item_id    INTEGER NOT NULL REFERENCES collection_state.item(id) ON DELETE CASCADE,
    id_type    TEXT NOT NULL,
    id_value   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (item_id, id_type, id_value)
);

CREATE INDEX IF NOT EXISTS item_identifiers_lookup_idx
    ON collection_state.item_identifiers (id_type, id_value);

-- ---------------------------------------------------------------------------
-- collection_item — relates table; an item can belong to multiple
-- collections. Cascades on delete of either side.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_state.collection_item (
    id            SERIAL PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES collection_state.collection(id) ON DELETE CASCADE,
    item_id       INTEGER NOT NULL REFERENCES collection_state.item(id) ON DELETE CASCADE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (collection_id, item_id)
);

CREATE INDEX IF NOT EXISTS collection_item_item_idx
    ON collection_state.collection_item (item_id);

-- ---------------------------------------------------------------------------
-- job_queue — pending work. A sensor claims a row by deleting it; no status
-- column (see header comment).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_state.job_queue (
    id            SERIAL PRIMARY KEY,
    collection_id INTEGER REFERENCES collection_state.collection(id) ON DELETE CASCADE,
    item_id       INTEGER REFERENCES collection_state.item(id) ON DELETE CASCADE,
    job_name      TEXT NOT NULL,
    config        JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(collection_id, item_id) = 1)
);

CREATE INDEX IF NOT EXISTS job_queue_poll_idx
    ON collection_state.job_queue (job_name, collection_id, item_id);

-- ---------------------------------------------------------------------------
-- job_result — exactly one row per (job_name, collection) or
-- (job_name, item), kept current via upsert_job_result(). Read by sensors
-- deciding what needs to run, Dagster partition creation, and Superset.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS collection_state.job_result (
    id            SERIAL PRIMARY KEY,
    collection_id INTEGER REFERENCES collection_state.collection(id) ON DELETE CASCADE,
    item_id       INTEGER REFERENCES collection_state.item(id) ON DELETE CASCADE,
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
    ON collection_state.job_result (job_name, collection_id) WHERE item_id IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS job_result_item_uq
    ON collection_state.job_result (job_name, item_id) WHERE collection_id IS NULL;

-- Upsert so there is ever only one job_result row per (job_name, target).
-- Picks whichever partial unique index above applies based on which of
-- p_collection_id / p_item_id is set.
CREATE OR REPLACE FUNCTION collection_state.upsert_job_result(
    p_job_name TEXT,
    p_collection_id INTEGER,
    p_item_id INTEGER,
    p_result JSONB
) RETURNS collection_state.job_result AS $$
DECLARE
    v_row collection_state.job_result;
BEGIN
    IF num_nonnulls(p_collection_id, p_item_id) != 1 THEN
        RAISE EXCEPTION 'upsert_job_result: exactly one of p_collection_id / p_item_id must be set';
    END IF;

    IF p_collection_id IS NOT NULL THEN
        INSERT INTO collection_state.job_result (job_name, collection_id, item_id, result, updated_at)
        VALUES (p_job_name, p_collection_id, NULL, p_result, now())
        ON CONFLICT (job_name, collection_id) WHERE item_id IS NULL
        DO UPDATE SET result = EXCLUDED.result, updated_at = now()
        RETURNING * INTO v_row;
    ELSE
        INSERT INTO collection_state.job_result (job_name, collection_id, item_id, result, updated_at)
        VALUES (p_job_name, NULL, p_item_id, p_result, now())
        ON CONFLICT (job_name, item_id) WHERE collection_id IS NULL
        DO UPDATE SET result = EXCLUDED.result, updated_at = now()
        RETURNING * INTO v_row;
    END IF;

    RETURN v_row;
END;
$$ LANGUAGE plpgsql;
