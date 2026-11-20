from fastapi import HTTPException

from src.config.database import Session
from src.models.dataset import Dataset
from src.services import storage
from src.utils.serialization import record


def _serialize(session, row: Dataset, include_backfill: bool = False) -> dict:
    data = record(row)
    # Surface the store selected by storage_mode. While both stores are
    # engaged they are kept equal by the dual write in ``save``.
    data['readings'] = storage.active_readings(session, row)
    data['storage_mode'] = row.storage_mode
    data['revision'] = row.revision
    if include_backfill:
        data['backfill'] = storage.backfill_state(session, row)
    return data


def list_items():
    with Session() as session:
        return [_serialize(session, row) for row in session.query(Dataset).order_by(Dataset.id.desc()).all()]


def detail(item_id: int):
    with Session() as session:
        row = session.get(Dataset, item_id)
        if not row:
            raise HTTPException(404, '未找到记录')
        return _serialize(session, row, include_backfill=True)


def save(data, item_id=None):
    payload = data.model_dump()
    readings = storage.normalize_readings(payload.get('readings'))
    with Session() as session:
        if item_id:
            row = storage.lock_dataset(session, item_id)
            if not row:
                raise HTTPException(404, '未找到记录')
        else:
            row = Dataset()
        engaged = bool(item_id) and storage.is_engaged(session, row)
        if item_id:
            row.name = payload['name']
            row.description = payload['description']
            row.sensors = payload['sensors']
            row.readings = [dict(item) for item in readings]
            row.revision = (row.revision or 0) + 1
        else:
            row = Dataset(name=payload['name'], description=payload['description'],
                          sensors=payload['sensors'], readings=[dict(item) for item in readings],
                          storage_mode='json', revision=0)
            session.add(row)
            session.flush()
        if engaged:
            # Dual write in the same transaction: concurrent add/remove/edit
            # during a backfill lands in both stores and is never overwritten
            # by a later batch.
            storage.replace_rows(session, row.id, readings)
        session.commit()
        session.refresh(row)
        return _serialize(session, row, include_backfill=engaged)


def start_backfill(item_id: int, batch_size: int = storage.DEFAULT_BATCH_SIZE):
    state = storage.start(item_id, batch_size)
    return state


def run_backfill(item_id: int, batches: int | None = 1):
    return storage.run(item_id, batches)


def retry_backfill(item_id: int):
    return storage.retry_last_batch(item_id)


def backfill_progress(item_id: int):
    with Session() as session:
        row = session.get(Dataset, item_id)
        if not row:
            raise HTTPException(404, '未找到数据集')
        return storage.backfill_state(session, row)


def source(item_id: int, limit: int = 2000):
    return storage.source_view(item_id, limit)


def switch(item_id: int):
    return storage.switch(item_id)


def rollback(item_id: int):
    return storage.rollback(item_id)
