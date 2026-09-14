# Preservica Identifier Conventions

A collection in Preservica is really just any folder (structural object) — there
is nothing inherently special about it. argonath tracks a folder as a collection
only once it carries a specific Preservica identifier; an unlabeled folder is
invisible to argonath's automatic sync sensor.

## The `argonath` identifier

Applied to the structural object that is the *root* of the collection — the same
entity passed as `ref` to `digtk preservica import`.

- **Type**: `argonath`
- **Value**: a comma-separated list of workflow tags, e.g. `sync`, `ai,public`,
  `sync,ai,public`.

| Tag      | Meaning                                                                 |
|----------|--------------------------------------------------------------------------|
| `sync`   | Track this collection and sync it into argonath bronze. Implied by any other tag — `argonath=ai,public` behaves identically to `argonath=sync,ai,public`. |
| `ai`     | Permit the AI metadata-draft stage (`generate_metadata`) to run for this collection's items. |
| `public` | Permit the public-dc/showcase publish step to run for this collection. |

The tag vocabulary is expected to grow as new optional pipeline stages come
online — an unrecognized tag is stored verbatim in the registry rather than
rejected.

## What the tag does — and doesn't — gate

Registration into argonath's collection registry (see below) does **not**
depend on this identifier at all: `digtk collection add` (disk) and `digtk
preservica import` (Preservica) always register a collection, labeled or not.
The `argonath` identifier only governs two things:

1. **Auto-discovery** — Dagster's `preservica_auto_sync_sensor` (polling
   Preservica's `/entities/updated-since`) will only auto-register a
   brand-new, never-before-seen folder if it carries this identifier. Once a
   Preservica collection is registered — whether by a human running `digtk
   preservica import` or by the sensor's own auto-discovery — it is **always**
   checked by the sensor going forward, regardless of its tags.
2. **Optional-stage gating** — `ai`/`public` permit those specific pipeline
   stages to run for the collection's items.

## Naming

The argonath collection name always mirrors the Preservica folder's own title
(slugified) — there is no separately-assigned slug. If registering a collection
would collide with a different, already-registered active collection's name,
registration is refused with an error rather than silently disambiguated: the
fix is renaming the source folder in Preservica (or picking a different name
for a manual disk collection via `digtk collection add`), not a generated
alternate slug.

## Storage: the collection registry

See `exec/digtk/sql/collection_registry.sql` for the schema (applied via
`digtk collection init-pg`) and `digtk.models.collection`
(`register_collection()`, `get_registration()`, `list_active_collections()`,
`find_changed_preservica_collections()`, `init_pg()`) for the logic that reads
and writes it. Tables live in their own `argonath` Postgres schema, namespaced
apart from anything else in the shared instance. Every known identifier for a
registered collection (the Preservica ref, ARK, LocalID/box-folder id, the raw
`argonath` tag value, etc.) is stored verbatim in `collection_identifiers` —
not just a single hardcoded ref column — so a collection can be looked up by
any of them.

Two Dagster sensors (`dagster/lib/sensors.py`) consume the registry:

- `preservica_auto_sync_sensor` — polls `/entities/updated-since` and
  reconciles only the registered collections that actually changed.
- `collection_partition_sync_sensor` — keeps Dagster's `collections` dynamic
  partition in sync with the registry, so a collection registered via CLI is
  immediately visible even before its first successful reconcile run.
