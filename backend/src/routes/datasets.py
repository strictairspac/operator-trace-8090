from fastapi import APIRouter, Depends

from src.middlewares.auth import current, writer
from src.schemas.business import DatasetInput
from src.schemas.storage import BackfillRun, BackfillStart
from src.services import datasets
from src.controllers.business import (save_dataset, start_dataset_backfill, advance_dataset_backfill,
                                      retry_dataset_backfill, read_dataset_source,
                                      switch_dataset_storage, rollback_dataset_storage)

router = APIRouter()


@router.get('/datasets')
def listing(u=Depends(current)):
    return datasets.list_items()


@router.post('/datasets')
def create(data: DatasetInput, u=Depends(writer)):
    return save_dataset(data, u)


@router.put('/datasets/{item_id}')
def update(item_id: int, data: DatasetInput, u=Depends(writer)):
    return save_dataset(data, u, item_id)


@router.get('/datasets/{item_id}')
def detail(item_id: int, u=Depends(current)):
    return datasets.detail(item_id)


@router.get('/datasets/{item_id}/storage/source')
def source(item_id: int, limit: int = 2000, u=Depends(current)):
    # Readable by viewers: compare the JSON source with the row store.
    return read_dataset_source(item_id, limit)


@router.post('/datasets/{item_id}/backfill')
def backfill_start(item_id: int, data: BackfillStart | None = None, u=Depends(writer)):
    return start_dataset_backfill(item_id, u, (data or BackfillStart()).batch_size)


@router.post('/datasets/{item_id}/backfill/run')
def backfill_run(item_id: int, data: BackfillRun | None = None, u=Depends(writer)):
    payload = data or BackfillRun()
    return advance_dataset_backfill(item_id, u, payload.batches)


@router.post('/datasets/{item_id}/backfill/retry')
def backfill_retry(item_id: int, u=Depends(writer)):
    return retry_dataset_backfill(item_id, u)


@router.get('/datasets/{item_id}/backfill')
def backfill_progress(item_id: int, u=Depends(current)):
    return datasets.backfill_progress(item_id)


@router.post('/datasets/{item_id}/storage/switch')
def storage_switch(item_id: int, u=Depends(writer)):
    return switch_dataset_storage(item_id, u)


@router.post('/datasets/{item_id}/storage/rollback')
def storage_rollback(item_id: int, u=Depends(writer)):
    return rollback_dataset_storage(item_id, u)
