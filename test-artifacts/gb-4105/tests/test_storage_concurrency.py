"""Real two-connection race tests: switch vs. trace creation / dataset edit."""
import threading
import time

import pytest

from src.config.database import Session
from src.models.dataset import Dataset
from src.models.reading import DatasetReading
from src.models.trace import Trace
from src.services import storage


def _complete_backfill(api, dataset):
    api.post(f'/datasets/{dataset.id}/backfill', json={'batch_size': 2})
    state = api.post(f'/datasets/{dataset.id}/backfill/run', json={'batches': None}).json()
    assert state['status'] == 'completed'


def _make_plan(session, dataset):
    from src.models.plan import Plan
    plan = Plan(name=f'race-plan-{time.time_ns()}', dataset_id=dataset.id,
                steps=[{'op': 'scan', 'source': 'readings'}], note='')
    session.add(plan)
    session.commit()
    session.refresh(plan)
    return plan


def test_switch_waits_for_in_flight_snapshot(api, session, dataset):
    """A snapshot taken under the shared lock finishes before the switch."""
    _complete_backfill(api, dataset)

    holder_ready = threading.Event()
    release_holder = threading.Event()
    snapshot = {}

    def hold_shared_and_snapshot():
        with Session() as s:
            row = storage.lock_dataset(s, dataset.id, share=True)
            snapshot['mode'] = row.storage_mode
            snapshot['readings'] = storage.active_readings(s, row)
            holder_ready.set()
            release_holder.wait(5)
            s.commit()

    holder = threading.Thread(target=hold_shared_and_snapshot)
    holder.start()
    assert holder_ready.wait(5)

    result = {}

    def do_switch():
        result['response'] = api.post(f'/datasets/{dataset.id}/storage/switch')

    switcher = threading.Thread(target=do_switch)
    switcher.start()
    time.sleep(0.4)
    assert switcher.is_alive(), 'switch must wait for the in-flight snapshot'
    release_holder.set()
    holder.join(5)
    switcher.join(5)

    assert result['response'].status_code == 200, result['response'].text
    # The in-flight reader saw the pre-switch version, coherently.
    assert snapshot['mode'] == 'json'
    assert [r['value'] for r in snapshot['readings']] == [float(r['value']) for r in dataset.readings]

    with Session() as s:
        assert s.get(Dataset, dataset.id).storage_mode == 'rows'


def test_snapshot_after_switch_reads_rows(api, session, dataset):
    """While the switch holds the exclusive lock, readers wait then read rows."""
    _complete_backfill(api, dataset)

    switched = api.post(f'/datasets/{dataset.id}/storage/switch')
    assert switched.status_code == 200

    with Session() as s:
        dataset = s.get(Dataset, dataset.id)
        expected = [float(r['value']) for r in dataset.readings]
        plan = _make_plan(s, dataset)
    latest = api.post('/traces', json={'name': '切换后取数', 'plan_id': plan.id})
    assert latest.status_code == 200, latest.text
    with Session() as s:
        trace = s.get(Trace, latest.json()['id'])
        assert trace.snapshot['storage_mode'] == 'rows'
        assert [r['value'] for r in trace.snapshot['dataset']['readings']] == expected


def test_trace_creation_waits_for_switch_then_sees_one_version(api, session, dataset):
    """Reverse order: exclusive switch lock blocks a concurrent trace snapshot."""
    _complete_backfill(api, dataset)
    plan = _make_plan(session, dataset)

    snapshot_done = threading.Event()
    results = {}

    def snapshot_only():
        with Session() as s:
            row = storage.lock_dataset(s, dataset.id, share=True)
            results['mode'] = row.storage_mode
            results['readings'] = storage.active_readings(s, row)
            s.commit()
        snapshot_done.set()

    # Hold the exclusive lock in this thread's transaction while another
    # thread tries to snapshot.
    with Session() as s:
        row = storage.lock_dataset(s, dataset.id, share=False)
        thread = threading.Thread(target=snapshot_only)
        thread.start()
        time.sleep(0.4)
        assert not snapshot_done.is_set(), 'snapshot must wait for the switch lock'
        row.storage_mode = 'rows'
        s.commit()
    thread.join(5)
    assert snapshot_done.is_set()
    assert results['mode'] == 'rows'
    assert [r['value'] for r in results['readings']] == [float(r['value']) for r in dataset.readings]


def test_edit_during_switch_is_not_lost(api, session, dataset):
    _complete_backfill(api, dataset)
    api.post(f'/datasets/{dataset.id}/storage/switch')

    # Edit through the normal API in row mode, then roll back.
    readings = session.query(DatasetReading).filter_by(dataset_id=dataset.id).order_by(
        DatasetReading.position).all()
    payload = [{'sensor_id': r.sensor_id, 'value': r.value, 'recorded_at': r.recorded_at}
               for r in readings]
    payload[0]['value'] = 12.5
    payload.append({'sensor_id': 2, 'value': 99, 'recorded_at': '2026-11-18T00:00:00'})
    edited = api.put(f'/datasets/{dataset.id}', json={
        'name': dataset.name, 'description': dataset.description,
        'sensors': dataset.sensors, 'readings': payload})
    assert edited.status_code == 200

    rolled = api.post(f'/datasets/{dataset.id}/storage/rollback').json()
    assert rolled['reading_total'] == len(payload)
    session.expire_all()
    assert session.get(Dataset, dataset.id).readings == payload
