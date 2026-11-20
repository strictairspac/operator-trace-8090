"""Row-per-reading storage: online backfill, verification, atomic switch, rollback.

Invariants
----------
* ``datasets.readings`` (legacy JSON array) and ``dataset_readings`` (one row
  per reading, ordered by a stable ``position``) are two representations of the
  same ordered list while a dataset is engaged in migration.
* As soon as a backfill is started for a dataset, every dataset edit writes
  *both* stores in one transaction (dual write).  Batched copying of the old
  data therefore never overwrites a concurrent add/remove/edit, and order is
  preserved because both stores are rewritten from the same ordered list.
* ``position`` is the identity of a reading.  Two readings with identical
  sensor/value/timestamp are two different positions and therefore two rows.
* Backfill advances by a position watermark (``cursor``).  Rows and cursor are
  committed in the same transaction, so an interrupted run resumes at the last
  committed batch.  Copying is an upsert on ``(dataset_id, position)``, so
  re-running the same batch inserts no duplicates.
* Trace creation takes a shared lock on the dataset and reads *one* store
  according to ``storage_mode``; switching/rollback take an exclusive lock.
  A new trace therefore sees exactly one version, and traces already queued
  keep the snapshot they captured at creation time.
* Switching is atomic and only happens after a row-by-row verification.
  Rolling back copies rows into the JSON store (rows are authoritative after a
  switch) before flipping the mode, so edits made after the switch survive.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from fastapi import HTTPException
from sqlalchemy import func, inspect, text
from sqlalchemy.exc import SQLAlchemyError

from src.config.database import Session
from src.models.dataset import Dataset
from src.models.reading import DatasetBackfill, DatasetReading
from src.utils.serialization import record

DEFAULT_BATCH_SIZE = 200
OPEN_BACKFILL_STATUSES = ('pending', 'running', 'paused', 'failed')
MIGRATION_READING_FIELDS = ('sensor_id', 'value', 'recorded_at')


# ---------------------------------------------------------------- helpers ---
def normalize_readings(raw: Iterable[dict] | None) -> list[dict]:
    """Canonical ordered reading list; numbers become floats for comparison."""
    result = []
    for item in raw or []:
        result.append({
            'sensor_id': int(item['sensor_id']),
            'value': float(item['value']),
            'recorded_at': str(item['recorded_at']),
        })
    return result


def reading_checksum(readings: Iterable[dict]) -> str:
    payload = json.dumps(list(readings), sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def source_readings(dataset: Dataset) -> list[dict]:
    return normalize_readings(dataset.readings)


def row_readings(session, dataset_id: int) -> list[dict]:
    rows = (session.query(DatasetReading)
            .filter_by(dataset_id=dataset_id)
            .order_by(DatasetReading.position).all())
    return [{
        'position': row.position,
        'sensor_id': int(row.sensor_id),
        'value': float(row.value),
        'recorded_at': row.recorded_at,
    } for row in rows]


def _bare(readings: Iterable[dict]) -> list[dict]:
    return [{field: item[field] for field in MIGRATION_READING_FIELDS} for item in readings]


def active_readings(session, dataset: Dataset, mode: str | None = None) -> list[dict]:
    """Read the single store selected by the dataset's current storage mode."""
    mode = mode or dataset.storage_mode or 'json'
    if mode == 'rows':
        return _bare(row_readings(session, dataset.id))
    return source_readings(dataset)


# ----------------------------------------------------------------- locking ---
def lock_dataset(session, dataset_id: int, share: bool = False) -> Dataset | None:
    query = session.query(Dataset).filter(Dataset.id == dataset_id)
    if session.get_bind().dialect.name != 'sqlite':
        query = query.with_for_update(read=share)
    return query.one_or_none()


def _require_dataset(session, dataset_id: int, share: bool = False) -> Dataset:
    dataset = lock_dataset(session, dataset_id, share=share)
    if not dataset:
        raise HTTPException(404, '未找到数据集')
    return dataset


def latest_backfill(session, dataset_id: int) -> DatasetBackfill | None:
    return (session.query(DatasetBackfill)
            .filter(DatasetBackfill.dataset_id == dataset_id)
            .order_by(DatasetBackfill.id.desc()).first())


def is_engaged(session, dataset: Dataset) -> bool:
    """True once migration has begun; edits then dual-write both stores."""
    return dataset.storage_mode == 'rows' or latest_backfill(session, dataset.id) is not None


# ------------------------------------------------------------ row writing ---
def replace_rows(session, dataset_id: int, readings: list[dict]) -> None:
    """Make the row store exactly mirror ``readings`` (ordered by list index)."""
    session.query(DatasetReading).filter_by(dataset_id=dataset_id).delete(synchronize_session=False)
    if readings:
        session.bulk_insert_mappings(DatasetReading, [{
            'dataset_id': dataset_id,
            'position': position,
            'sensor_id': item['sensor_id'],
            'value': item['value'],
            'recorded_at': item['recorded_at'],
        } for position, item in enumerate(readings)])


def _upsert_positions(session, dataset_id: int, readings: list[dict], start: int) -> None:
    """Idempotent batch copy: re-running a batch updates instead of duplicating."""
    if not readings:
        return
    records = [{
        'dataset_id': dataset_id,
        'position': start + offset,
        'sensor_id': item['sensor_id'],
        'value': item['value'],
        'recorded_at': item['recorded_at'],
    } for offset, item in enumerate(readings)]
    table = DatasetReading.__table__
    dialect = session.get_bind().dialect.name
    set_ = None
    if dialect == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
        statement = dialect_insert(table).values(records)
        set_ = {'sensor_id': statement.excluded.sensor_id,
                'value': statement.excluded.value,
                'recorded_at': statement.excluded.recorded_at}
        session.execute(statement.on_conflict_do_update(
            index_elements=['dataset_id', 'position'], set_=set_))
        return
    if dialect == 'sqlite':
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
        statement = dialect_insert(table).values(records)
        set_ = {'sensor_id': statement.excluded.sensor_id,
                'value': statement.excluded.value,
                'recorded_at': statement.excluded.recorded_at}
        session.execute(statement.on_conflict_do_update(
            index_elements=['dataset_id', 'position'], set_=set_))
        return
    positions = [item['position'] for item in records]
    session.query(DatasetReading).filter(
        DatasetReading.dataset_id == dataset_id,
        DatasetReading.position.in_(positions)).delete(synchronize_session=False)
    session.bulk_insert_mappings(DatasetReading, records)


def _delete_positions_at_or_after(session, dataset_id: int, total: int) -> None:
    session.query(DatasetReading).filter(
        DatasetReading.dataset_id == dataset_id,
        DatasetReading.position >= total).delete(synchronize_session=False)


def _mismatches(source: list[dict], rows: list[dict]) -> list[int]:
    by_position = {row['position']: row for row in rows}
    found = []
    for position, item in enumerate(source):
        row = by_position.get(position)
        if row is None or any(row[field] != item[field] for field in MIGRATION_READING_FIELDS):
            found.append(position)
    found.extend(position for position in by_position if position >= len(source))
    return sorted(set(found))


# ------------------------------------------------------------ verification ---
def verify(session, dataset: Dataset, mismatch_limit: int = 50) -> dict:
    """Row-by-row comparison of the JSON source and the row store."""
    source = source_readings(dataset)
    rows = row_readings(session, dataset.id)
    by_position = {row['position']: row for row in rows}
    mismatches = []
    for position, item in enumerate(source):
        row = by_position.get(position)
        if row is None:
            mismatches.append({'position': position, 'reason': '逐行表缺少此行'})
        elif any(row[field] != item[field] for field in MIGRATION_READING_FIELDS):
            mismatches.append({'position': position, 'reason': '内容不一致',
                               'source': item,
                               'row': {field: row[field] for field in MIGRATION_READING_FIELDS}})
    for position in sorted(by_position):
        if position >= len(source):
            mismatches.append({'position': position, 'reason': '逐行表多出此行'})
    return {
        'ok': not mismatches,
        'source_total': len(source),
        'row_total': len(rows),
        'source_checksum': reading_checksum(source),
        'row_checksum': reading_checksum(_bare(rows)),
        'mismatch_count': len(mismatches),
        'mismatches': mismatches[:mismatch_limit],
    }


# ---------------------------------------------------------------- backfill ---
def backfill_state(session, dataset: Dataset) -> dict:
    backfill = latest_backfill(session, dataset.id)
    row_count = session.query(func.count(DatasetReading.id)).filter_by(dataset_id=dataset.id).scalar() or 0
    source_total = len(source_readings(dataset))
    base = {
        'dataset_id': dataset.id,
        'storage_mode': dataset.storage_mode,
        'revision': dataset.revision,
        'source_total': source_total,
        'row_count': row_count,
        'engaged': backfill is not None or dataset.storage_mode == 'rows',
    }
    if not backfill:
        return {**base, 'id': None, 'status': 'not_started', 'batch_size': DEFAULT_BATCH_SIZE,
                'cursor': 0, 'copied_rows': 0, 'last_batch_size': 0, 'batches_run': 0,
                'checksum': None, 'error': None, 'progress': 0.0,
                'updated_at': None, 'created_at': None}
    state = record(backfill)
    state.update(base)
    state['source_total'] = backfill.source_total or source_total
    total = state['source_total']
    if backfill.status in ('completed', 'rolled_back'):
        progress = 100.0
    elif total <= 0:
        progress = 100.0 if backfill.status == 'completed' else 0.0
    else:
        progress = round(min(100.0, (backfill.cursor or 0) * 100.0 / total), 1)
    state['progress'] = progress
    return state


def start(dataset_id: int, batch_size: int = DEFAULT_BATCH_SIZE) -> dict:
    batch_size = max(1, min(int(batch_size or DEFAULT_BATCH_SIZE), 2000))
    with Session() as session:
        dataset = _require_dataset(session, dataset_id)
        if dataset.storage_mode == 'rows':
            raise HTTPException(409, '数据集已处于逐行存储；如需重做请先回退')
        active = (session.query(DatasetBackfill)
                  .filter(DatasetBackfill.dataset_id == dataset.id,
                          DatasetBackfill.status.in_(OPEN_BACKFILL_STATUSES))
                  .order_by(DatasetBackfill.id.desc()).first())
        if active:
            return backfill_state(session, dataset)
        backfill = DatasetBackfill(
            dataset_id=dataset.id, status='pending', batch_size=batch_size,
            cursor=0, source_total=len(source_readings(dataset)), copied_rows=0,
            last_batch_size=0, batches_run=0,
        )
        session.add(backfill)
        session.commit()
        session.refresh(dataset)
        return backfill_state(session, dataset)


def _reconcile(session, dataset: Dataset, source: list[dict]) -> None:
    """Repair missing/mismatched positions and drop positions past the end.

    The target table must be a full copy of the source before switching, so a
    concurrent delete that left a hole is repaired inside the last batch's
    transaction.  Idempotent.
    """
    if _mismatches(source, row_readings(session, dataset.id)):
        replace_rows(session, dataset.id, source)
    else:
        _delete_positions_at_or_after(session, dataset.id, len(source))


def _copy_batch(session, dataset: Dataset, start_at: int, end_at: int) -> None:
    source = source_readings(dataset)
    if end_at > start_at:
        _upsert_positions(session, dataset.id, source[start_at:end_at], start_at)


def _run_one(dataset_id: int) -> tuple[bool, dict]:
    """Execute exactly one committed batch. Returns ``(finished, state)``."""
    with Session() as session:
        dataset = _require_dataset(session, dataset_id)
        backfill = latest_backfill(session, dataset.id)
        if not backfill:
            raise HTTPException(409, '请先发起回填')
        if session.get_bind().dialect.name != 'sqlite':
            backfill = (session.query(DatasetBackfill).filter_by(id=backfill.id)
                        .with_for_update().one())
        if backfill.status == 'rolled_back':
            raise HTTPException(409, '该回填已回退，请重新发起回填')
        if dataset.storage_mode == 'rows':
            return True, backfill_state(session, dataset)
        if backfill.status == 'completed':
            # Already copied once, but a row may have drifted since (e.g. a
            # direct delete on the target).  Re-verify before declaring done;
            # on mismatch rewind one batch so the repair path re-copies and
            # reconciles rather than switching over inconsistent data.
            report = verify(session, dataset)
            if report['ok']:
                return True, backfill_state(session, dataset)
            backfill.status = 'paused'
            backfill.cursor = max(0, (backfill.cursor or 0) - (backfill.last_batch_size or backfill.batch_size))
        try:
            source = source_readings(dataset)
            total = len(source)
            start_at = min(backfill.cursor or 0, total)
            end_at = min(start_at + (backfill.batch_size or DEFAULT_BATCH_SIZE), total)
            backfill.status = 'running'
            backfill.source_total = total
            backfill.error = None
            _copy_batch(session, dataset, start_at, end_at)
            backfill.cursor = end_at
            backfill.copied_rows = end_at
            backfill.last_batch_size = end_at - start_at
            backfill.batches_run = (backfill.batches_run or 0) + 1
            if end_at >= total:
                _reconcile(session, dataset, source)
                report = verify(session, dataset)
                if not report['ok']:
                    raise HTTPException(409, f"逐条核对未通过：{report['mismatch_count']} 处不一致")
                backfill.status = 'completed'
                backfill.copied_rows = total
                backfill.checksum = report['source_checksum']
            else:
                backfill.status = 'paused'
            session.commit()
            session.refresh(dataset)
            return backfill.status == 'completed', backfill_state(session, dataset)
        except HTTPException:
            session.rollback()
            _mark_failed(dataset_id, backfill.id, '逐条核对未通过')
            raise
        except SQLAlchemyError as exc:
            session.rollback()
            _mark_failed(dataset_id, backfill.id, str(exc))
            raise HTTPException(500, f'回填批次失败，进度未前进：{exc}') from exc
        except Exception as exc:  # noqa: BLE001 - report and keep progress
            session.rollback()
            _mark_failed(dataset_id, backfill.id, str(exc))
            raise HTTPException(500, f'回填批次失败，进度未前进：{exc}') from exc


def run(dataset_id: int, count: int | None = 1) -> dict:
    """Run ``count`` batches (``None`` = until complete), one transaction each.

    Committing per batch is what makes an interrupted backfill resumable from
    the previous batch: rows and cursor always move together.
    """
    finished = False
    state: dict = {}
    processed = 0
    while not finished and (count is None or processed < count):
        finished, state = _run_one(dataset_id)
        processed += 1
    if not state:
        with Session() as session:
            state = backfill_state(session, _require_dataset(session, dataset_id))
    return state


def retry_last_batch(dataset_id: int) -> dict:
    """Re-copy the last committed batch; upsert makes it duplicate-free."""
    with Session() as session:
        dataset = _require_dataset(session, dataset_id)
        backfill = latest_backfill(session, dataset.id)
        if not backfill:
            raise HTTPException(409, '请先发起回填')
        if backfill.status in ('completed', 'rolled_back'):
            raise HTTPException(409, '回填已结束，无需重试')
        if session.get_bind().dialect.name != 'sqlite':
            backfill = (session.query(DatasetBackfill).filter_by(id=backfill.id)
                        .with_for_update().one())
        size = backfill.last_batch_size or backfill.batch_size or DEFAULT_BATCH_SIZE
        cursor = backfill.cursor or 0
        start_at = max(0, cursor - size)
        source = source_readings(dataset)
        end_at = min(cursor, len(source))
        _copy_batch(session, dataset, start_at, end_at)
        backfill.error = None
        backfill.last_batch_size = end_at - start_at
        backfill.batches_run = (backfill.batches_run or 0) + 1
        # The batch is re-copied; what remains is the final verification, which
        # the next ``run`` performs.  A successful retry is therefore paused,
        # never failed.
        backfill.status = 'paused'
        session.commit()
        session.refresh(dataset)
        state = backfill_state(session, dataset)
        state['retried_batch'] = {'from': start_at, 'to': end_at}
        return state


def _mark_failed(dataset_id: int, backfill_id: int, error: str) -> None:
    """Record a failure without moving the cursor (resumable retry)."""
    try:
        with Session() as session:
            backfill = session.get(DatasetBackfill, backfill_id)
            if not backfill or backfill.status in ('completed', 'rolled_back'):
                return
            backfill.status = 'failed'
            backfill.error = error[:1000]
            session.commit()
    except Exception:  # noqa: BLE001 - never mask the original failure
        pass


# --------------------------------------------------------- switch/rollback ---
def switch(dataset_id: int) -> dict:
    with Session() as session:
        dataset = _require_dataset(session, dataset_id)  # exclusive lock
        if dataset.storage_mode == 'rows':
            return {'dataset': record(dataset), 'verify': verify(session, dataset),
                    'already_switched': True}
        backfill = latest_backfill(session, dataset.id)
        if not backfill:
            raise HTTPException(409, '请先完成回填再切换')
        if backfill.status != 'completed':
            raise HTTPException(409, '回填尚未逐条核齐，不能切换')
        report = verify(session, dataset)
        if not report['ok']:
            raise HTTPException(409, f"逐条核对未通过：{report['mismatch_count']} 处不一致，不能切换")
        dataset.storage_mode = 'rows'
        dataset.revision = (dataset.revision or 0) + 1
        session.commit()
        session.refresh(dataset)
        return {'dataset': record(dataset), 'verify': report, 'already_switched': False}


def rollback(dataset_id: int) -> dict:
    with Session() as session:
        dataset = _require_dataset(session, dataset_id)  # exclusive lock
        if dataset.storage_mode != 'rows':
            return {'dataset': record(dataset), 'already_rolled_back': True}
        # Rows are authoritative after the switch: copy them into JSON before
        # flipping back so edits made in row mode are neither lost nor
        # overwritten by a stale JSON copy.
        readings = active_readings(session, dataset, mode='rows')
        dataset.readings = [dict(item) for item in readings]
        dataset.storage_mode = 'json'
        dataset.revision = (dataset.revision or 0) + 1
        backfill = latest_backfill(session, dataset.id)
        if backfill and backfill.status == 'completed':
            backfill.status = 'rolled_back'
        session.commit()
        session.refresh(dataset)
        return {'dataset': record(dataset), 'already_rolled_back': False,
                'reading_total': len(readings)}


# --------------------------------------------------------------- read view ---
def source_view(dataset_id: int, limit: int = 2000) -> dict:
    limit = max(1, min(int(limit or 2000), 2000))
    with Session() as session:
        dataset = session.get(Dataset, dataset_id)
        if not dataset:
            raise HTTPException(404, '未找到数据集')
        source = source_readings(dataset)
        rows = row_readings(session, dataset.id)
        return {
            'dataset_id': dataset.id,
            'name': dataset.name,
            'storage_mode': dataset.storage_mode,
            'revision': dataset.revision,
            'engaged': is_engaged(session, dataset),
            'source': {'total': len(source), 'checksum': reading_checksum(source),
                       'readings': source[:limit]},
            'rows': {'total': len(rows), 'checksum': reading_checksum(_bare(rows)),
                     'readings': rows[:limit]},
        }


# ------------------------------------------------------------ schema guard ---
def ensure_schema() -> None:
    """Idempotently add migration columns/tables to pre-migration databases."""
    from src.config.database import Base, engine
    import src.models.dataset  # noqa: F401 - register tables
    import src.models.reading  # noqa: F401
    Base.metadata.create_all(engine)
    columns = {column['name'] for column in inspect(engine).get_columns('datasets')}
    statements = []
    if 'storage_mode' not in columns:
        statements.append("ALTER TABLE datasets ADD COLUMN storage_mode VARCHAR(20) NOT NULL DEFAULT 'json'")
    if 'revision' not in columns:
        statements.append("ALTER TABLE datasets ADD COLUMN revision INTEGER NOT NULL DEFAULT 0")
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
