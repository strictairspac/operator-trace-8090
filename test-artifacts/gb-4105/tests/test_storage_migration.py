import json

from src.models.dataset import Dataset
from src.models.reading import DatasetBackfill, DatasetReading
from src.services import storage


def _read_row_positions(session, dataset_id):
    return [(r.position, r.sensor_id, r.value, r.recorded_at)
            for r in session.query(DatasetReading).filter_by(dataset_id=dataset_id)
            .order_by(DatasetReading.position).all()]


def test_backfill_preserves_order_and_duplicate_values(api, session, dataset):
    state = api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 2}).json()
    assert state['status'] == 'pending'
    assert state['source_total'] == 5

    progressed = api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': 1}).json()
    assert progressed['cursor'] == 2
    assert progressed['progress'] == 40.0

    final = api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': None}).json()
    assert final['status'] == 'completed'
    assert final['cursor'] == 5

    rows = _read_row_positions(session, dataset.id)
    source = [(i, r['sensor_id'], float(r['value']), r['recorded_at'])
              for i, r in enumerate(dataset.readings)]
    assert rows == source
    # duplicate identical readings survive as two rows
    assert rows[0][1:] == rows[1][1:]
    assert rows[0][0] == 0 and rows[1][0] == 1


def test_interrupted_backfill_resumes_from_last_batch(api, session, dataset):
    api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 2})
    api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': 1})
    before = _read_row_positions(session, dataset.id)
    assert [p for p, *_ in before] == [0, 1]

    # simulate an interruption: cursor/rows committed, status stuck in running
    row = session.query(DatasetBackfill).filter_by(dataset_id=dataset.id).one()
    row.status = 'running'
    session.commit()

    resumed = api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': None}).json()
    assert resumed['status'] == 'completed'
    positions = [p for p, *_ in _read_row_positions(session, dataset.id)]
    assert positions == [0, 1, 2, 3, 4]


def test_retrying_same_batch_does_not_duplicate(api, session, dataset):
    api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 2})
    api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': 1})
    assert len(_read_row_positions(session, dataset.id)) == 2

    first = api.post(f'/datasets/{dataset.id}/backfill/retry').json()
    second = api.post(f'/datasets/{dataset.id}/backfill/retry').json()
    assert first['retried_batch'] == {'from': 0, 'to': 2}
    assert second['retried_batch'] == {'from': 0, 'to': 2}
    positions = [p for p, *_ in _read_row_positions(session, dataset.id)]
    assert positions == [0, 1]


def test_concurrent_edit_during_backfill_is_kept(api, session, dataset):
    api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 2})
    api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': 1})

    edited = list(dataset.readings)
    del edited[0]                                   # delete a row
    edited.append({'sensor_id': 2, 'value': 61, 'recorded_at': '2026-11-17T10:00:00'})  # add
    edited[1]['value'] = 41                          # change
    payload = {
        'name': dataset.name,
        'description': dataset.description,
        'sensors': dataset.sensors,
        'readings': edited,
    }
    response = api.put(f'/datasets/{dataset.id}', json=payload)
    assert response.status_code == 200, response.text

    final = api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': None}).json()
    assert final['status'] == 'completed'
    rows = _read_row_positions(session, dataset.id)
    expected = [(i, float(r['value']), r['recorded_at']) for i, r in enumerate(edited)]
    assert [(p, v, t) for p, _, v, t in rows] == expected

    report = api.get(f'/datasets/{dataset.id}/storage/source').json()
    assert report['source']['checksum'] == report['rows']['checksum']
    assert report['source']['total'] == report['rows']['total'] == len(edited)


def test_delete_during_backfill_is_repaired_on_finalize(api, session, dataset):
    api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 2})
    api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': 1})

    # Delete the very last reading after its position was already targeted by
    # an earlier edit; the finalize step must drop the stale tail row.
    shortened = list(dataset.readings)[:4]
    api.put(f'/datasets/{dataset.id}', json={
        'name': dataset.name, 'description': dataset.description,
        'sensors': dataset.sensors, 'readings': shortened,
    })
    # Force a stale extra row into the target, as a concurrent delete could.
    session.add(DatasetReading(dataset_id=dataset.id, position=4,
                               sensor_id=9, value=1, recorded_at='stale'))
    session.commit()

    final = api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': None}).json()
    assert final['status'] == 'completed'
    positions = [p for p, *_ in _read_row_positions(session, dataset.id)]
    assert positions == [0, 1, 2, 3]


def test_switch_requires_full_verification(api, session, dataset):
    api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 2})
    early = api.post(f'/datasets/{dataset.id}/storage/switch')
    assert early.status_code == 409

    api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': None})
    # Tamper with a row: verification must block the switch.
    row = session.query(DatasetReading).filter_by(dataset_id=dataset.id, position=3).one()
    row.value = 999
    session.commit()
    blocked = api.post(f'/datasets/{dataset.id}/storage/switch')
    assert blocked.status_code == 409
    assert '不一致' in blocked.json()['detail']

    session.query(DatasetReading).filter_by(dataset_id=dataset.id, position=3).delete()
    session.commit()
    repaired = api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': 1}).json()
    assert repaired['status'] == 'completed'

    switched = api.post(f'/datasets/{dataset.id}/storage/switch')
    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert body['verify']['ok'] is True
    assert body['dataset']['storage_mode'] == 'rows'


def test_rollback_keeps_post_switch_edits(api, session, dataset):
    api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 5})
    api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': None})
    api.post(f'/datasets/{dataset.id}/storage/switch')

    readings = api.get(f'/datasets/{dataset.id}/storage/source').json()['rows']['readings']
    readings = [{'sensor_id': r['sensor_id'], 'value': r['value'], 'recorded_at': r['recorded_at']}
                for r in readings]
    readings[2]['value'] = 77.25
    readings.append({'sensor_id': 2, 'value': 30, 'recorded_at': '2026-11-17T11:00:00'})
    del readings[0]
    edited = api.put(f'/datasets/{dataset.id}', json={
        'name': dataset.name, 'description': dataset.description,
        'sensors': dataset.sensors, 'readings': readings,
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()['storage_mode'] == 'rows'

    rolled = api.post(f'/datasets/{dataset.id}/storage/rollback')
    assert rolled.status_code == 200
    body = rolled.json()
    assert body['dataset']['storage_mode'] == 'json'
    assert body['reading_total'] == len(readings)

    session.expire_all()
    stored = session.get(Dataset, dataset.id)
    assert stored.readings == readings
    rows = _read_row_positions(session, dataset.id)
    assert [(v, t) for _, _, v, t in rows] == [(float(r['value']), r['recorded_at']) for r in readings]


def test_traces_read_one_version_and_queued_traces_keep_snapshot(api, session, dataset):
    from src.models.plan import Plan
    from src.models.trace import Trace

    plan = Plan(name='迁移测试计划', dataset_id=dataset.id, steps=[{'op': 'scan', 'source': 'readings'}], note='')
    session.add(plan)
    session.commit()
    session.refresh(plan)

    queued = api.post('/traces', json={'name': '切换前', 'plan_id': plan.id})
    assert queued.status_code == 200, queued.text
    before = session.get(Trace, queued.json()['id'])
    before_readings = before.snapshot['dataset']['readings']
    assert before.snapshot['storage_mode'] == 'json'

    api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 5})
    api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': None})
    api.post(f'/datasets/{dataset.id}/storage/switch')

    new_trace = api.post('/traces', json={'name': '切换后', 'plan_id': plan.id})
    assert new_trace.status_code == 200, new_trace.text
    after = session.get(Trace, new_trace.json()['id'])
    assert after.snapshot['storage_mode'] == 'rows'
    assert [r['value'] for r in after.snapshot['dataset']['readings']] == \
           [float(r['value']) for r in before_readings]

    session.expire_all()
    assert session.get(Trace, before.id).snapshot == before.snapshot

    api.post(f'/datasets/{dataset.id}/storage/rollback')
    after_rollback = api.post('/traces', json={'name': '回退后', 'plan_id': plan.id}).json()
    rolled_trace = session.get(Trace, after_rollback['id'])
    assert rolled_trace.snapshot['storage_mode'] == 'json'


def test_viewer_cannot_backfill(api, dataset):
    response = api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 2},
                        headers=api.viewer_headers)
    assert response.status_code == 403
    readable = api.get(f'/datasets/{dataset.id}/storage/source', headers=api.viewer_headers)
    assert readable.status_code == 200
