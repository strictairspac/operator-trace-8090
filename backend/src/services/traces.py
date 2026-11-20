import redis
from fastapi import HTTPException

from src.config.database import Session
from src.config.settings import REDIS_URL
from src.models.plan import Plan
from src.models.trace import Trace
from src.services import storage
from src.utils.serialization import record

queue = redis.Redis.from_url(REDIS_URL)


def list_items():
    with Session() as session:
        return [record(row) for row in session.query(Trace).order_by(Trace.id.desc()).limit(100).all()]


def detail(item_id):
    with Session() as session:
        row = session.get(Trace, item_id)
        if not row:
            raise HTTPException(404, '未找到执行轨迹')
        return record(row)


def create(data):
    with Session() as session:
        plan = session.get(Plan, data.plan_id)
        if not plan:
            raise HTTPException(404, '未找到算子计划')
        # Shared lock: the snapshot below is read from exactly one store while
        # a switch/backfill (exclusive lock) cannot interleave.  A trace queued
        # before the switch keeps this snapshot; traces created afterwards read
        # the newly selected store.
        dataset = storage.lock_dataset(session, plan.dataset_id, share=True)
        if not dataset:
            raise HTTPException(404, '未找到算子计划')
        dataset_payload = record(dataset)
        dataset_payload['readings'] = storage.active_readings(session, dataset)
        row = Trace(name=data.name, plan_id=plan.id, dataset_id=dataset.id, status='queued',
                    snapshot={'dataset': dataset_payload, 'plan': record(plan),
                              'storage_mode': dataset.storage_mode, 'revision': dataset.revision})
        session.add(row)
        session.commit()
        session.refresh(row)
        result = record(row)
    try:
        queue.rpush('operator-traces', result['id'])
    except redis.RedisError:
        with Session() as session:
            failed = session.get(Trace, result['id'])
            failed.status = 'failed'
            failed.error = '队列不可用，请创建新任务'
            session.commit()
        raise HTTPException(503, '队列不可用，请稍后重试')
    return result
