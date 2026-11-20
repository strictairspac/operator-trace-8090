from src.services import datasets, plans, traces, storage
from src.services.audit import log


def save_dataset(data, user, item_id=None):
    row = datasets.save(data, item_id)
    log(user.email, 'dataset.save', row['name'])
    return row


def save_plan(data, user, item_id=None):
    row = plans.save(data, item_id)
    log(user.email, 'plan.save', row['name'])
    return row


def start_trace(data, user):
    row = traces.create(data)
    log(user.email, 'trace.queue', row['name'])
    return row


def start_dataset_backfill(item_id, user, batch_size=storage.DEFAULT_BATCH_SIZE):
    state = datasets.start_backfill(item_id, batch_size)
    log(user.email, 'dataset.backfill.start', f'{item_id}:{state.get("id")}')
    return state


def advance_dataset_backfill(item_id, user, batches=1):
    state = datasets.run_backfill(item_id, batches)
    log(user.email, 'dataset.backfill.run', f'{item_id}:{state.get("id")}:{state.get("status")}')
    return state


def retry_dataset_backfill(item_id, user):
    state = datasets.retry_backfill(item_id)
    log(user.email, 'dataset.backfill.retry', f'{item_id}:{state.get("id")}')
    return state


def read_dataset_source(item_id, limit=2000):
    return datasets.source(item_id, limit)


def switch_dataset_storage(item_id, user):
    result = datasets.switch(item_id)
    log(user.email, 'dataset.storage.switch', f'{item_id}:rows')
    return result


def rollback_dataset_storage(item_id, user):
    result = datasets.rollback(item_id)
    log(user.email, 'dataset.storage.rollback', f'{item_id}:json')
    return result
