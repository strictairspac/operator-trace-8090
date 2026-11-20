# Schema v1
SQLAlchemy metadata creates users, audits, datasets, plans, traces. Natural names are unique; foreign keys reference parent entities. Each trace stores immutable JSON input snapshots. PostgreSQL persists metadata and teaching datasets; the bounded Python operators execute the plan.

# Schema v2 (row-per-reading storage)

Adds `datasets.storage_mode` (`json`|`rows`) and `datasets.revision`; adds
`dataset_readings` (one row per reading, unique on `(dataset_id, position)`,
FK cascade) and `dataset_backfills` (resumable copy state). `ensure_schema()`
runs at startup and upgrades v1 databases in place; it is idempotent.

- `position` is assigned from source order and never reused for a different
  reading, so identical readings stay separate rows and the order is stable.
- While a dataset is engaged in migration (`storage_mode='rows'` or a backfill
  exists) dataset edits dual-write JSON and rows in one transaction.
- Backfill commits rows and cursor together per batch; copies upsert on
  `(dataset_id, position)`, so resuming or retrying a batch adds no duplicates.
- Switch and rollback take an exclusive row lock on the dataset; trace creation
  takes a shared lock and reads exactly one store, so a snapshot is one version.
- Switching requires a full row-by-row verification. Rollback writes rows back
  into JSON first, so post-switch edits are preserved.
