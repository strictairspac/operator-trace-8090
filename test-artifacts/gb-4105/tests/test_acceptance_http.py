"""Acceptance-style end-to-end checks against a running server.

Run with: ``DATABASE_URL=... python -m pytest tests/test_acceptance_http.py``
Environment (``ACCEPT_BASE_URL``) points at a live backend.  These tests use
real HTTP so they also cover route wiring and auth.
"""
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

BASE = os.getenv('ACCEPT_BASE_URL', 'http://127.0.0.1:18245')


def _call(method, path, token=None, body=None):
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json', **({'Authorization': 'Bearer ' + token} if token else {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b'{}')


@pytest.fixture(scope='module')
def tokens():
    status, data = _call('POST', '/auth/login',
                         body={'email': 'operator@operator.local', 'password': 'Trace2026!'})
    if status != 200:
        pytest.skip('live server is not seeded')
    return data['token']


@pytest.fixture()
def fresh_dataset(tokens):
    """A 60-reading dataset with a deliberate duplicate pair, plus its plan."""
    readings = [{'sensor_id': i % 3 + 1, 'value': 20 + i, 'recorded_at': f'2026-11-19T10:{i:02d}:00'}
                for i in range(60)]
    readings[1] = dict(readings[0])  # duplicate values, distinct positions
    status, data = _call('POST', '/datasets', tokens, {
        'name': f'验收-{time.time_ns()}',
        'description': '验收数据集',
        'sensors': [{'sensor_id': 1, 'zone': '北场', 'device_type': 'pump'}],
        'readings': readings,
    })
    assert status == 200, data
    status, plan = _call('POST', '/plans', tokens, {
        'name': f"验收计划-{time.time_ns()}",
        'dataset_id': data['id'],
        'steps': [{'op': 'scan', 'source': 'readings'}],
        'note': '验收',
    })
    assert status == 200, plan
    data['plan_id'] = plan['id']
    return data


def test_acceptance_backfill_then_delete_during_run(tokens, fresh_dataset):
    dataset_id = fresh_dataset['id']
    status, state = _call('POST', f'/datasets/{dataset_id}/backfill', tokens, {'batch_size': 20})
    assert status == 200 and state['status'] == 'pending'

    status, state = _call('POST', f'/datasets/{dataset_id}/backfill/run', tokens, {'batches': 1})
    assert state['cursor'] == 20 and state['progress'] == pytest.approx(33.3, abs=0.1)

    # Concurrent delete while the copy is mid-flight: remove a copied row and
    # a not-yet-copied row, then finish the copy.
    status, row = _call('GET', f'/datasets/{dataset_id}', tokens)
    readings = row['readings']
    del readings[5]      # already copied
    del readings[40]     # beyond the watermark
    status, edited = _call('PUT', f'/datasets/{dataset_id}', tokens, {
        'name': row['name'], 'description': row['description'],
        'sensors': row['sensors'], 'readings': readings,
    })
    assert status == 200, edited
    assert len(edited['readings']) == 58

    status, final = _call('POST', f'/datasets/{dataset_id}/backfill/run', tokens, {'batches': None})
    assert status == 200 and final['status'] == 'completed'
    assert final['cursor'] == final['source_total'] == 58
    assert final['row_count'] == 58

    status, compare = _call('GET', f'/datasets/{dataset_id}/storage/source', tokens)
    assert compare['source']['total'] == compare['rows']['total'] == 58
    assert compare['source']['checksum'] == compare['rows']['checksum']
    # The duplicated pair is still two rows with distinct positions.
    assert compare['rows']['readings'][0]['value'] == compare['rows']['readings'][1]['value']
    assert compare['rows']['readings'][0]['position'] != compare['rows']['readings'][1]['position']


def test_acceptance_switch_races_trace_creation(tokens, fresh_dataset):
    dataset_id = fresh_dataset['id']
    _call('POST', f'/datasets/{dataset_id}/backfill', tokens, {'batch_size': 15})
    _call('POST', f'/datasets/{dataset_id}/backfill/run', tokens, {'batches': None})

    plan_id = fresh_dataset['plan_id']

    outcome = {'switch': None, 'trace': None}

    def do_switch():
        outcome['switch'] = _call('POST', f'/datasets/{dataset_id}/storage/switch', tokens)

    def do_trace():
        time.sleep(0.02)
        outcome['trace'] = _call('POST', '/traces', tokens,
                                 {'name': f'撞取数-{time.time_ns()}', 'plan_id': plan_id})

    switcher = threading.Thread(target=do_switch)
    reader = threading.Thread(target=do_trace)
    switcher.start(), reader.start()
    switcher.join(30), reader.join(30)

    assert outcome['switch'][0] == 200, outcome['switch']
    assert outcome['trace'][0] == 200, outcome['trace']
    status, trace = _call('GET', f"/traces/{outcome['trace'][1]['id']}", tokens)
    snapshot = trace['snapshot']
    assert snapshot['storage_mode'] in ('json', 'rows')
    # Whichever version it captured, it is internally consistent with that one
    # store: the snapshot length equals that version's row count.
    assert len(snapshot['dataset']['readings']) == 60

    status, fresh = _call('GET', f'/datasets/{dataset_id}', tokens)
    assert fresh['storage_mode'] == 'rows'


def test_acceptance_edit_after_switch_then_rollback(tokens, fresh_dataset):
    dataset_id = fresh_dataset['id']
    _call('POST', f'/datasets/{dataset_id}/backfill', tokens, {'batch_size': 100})
    _call('POST', f'/datasets/{dataset_id}/backfill/run', tokens, {'batches': None})
    status, switched = _call('POST', f'/datasets/{dataset_id}/storage/switch', tokens)
    assert status == 200 and switched['dataset']['storage_mode'] == 'rows'

    status, row = _call('GET', f'/datasets/{dataset_id}', tokens)
    readings = row['readings']
    readings[0]['value'] = 999.5
    readings.append({'sensor_id': 3, 'value': 7, 'recorded_at': '2026-11-19T23:59:00'})
    del readings[10]
    status, edited = _call('PUT', f'/datasets/{dataset_id}', tokens, {
        'name': row['name'], 'description': row['description'],
        'sensors': row['sensors'], 'readings': readings,
    })
    assert status == 200 and edited['storage_mode'] == 'rows'

    status, rolled = _call('POST', f'/datasets/{dataset_id}/storage/rollback', tokens)
    assert status == 200 and rolled['dataset']['storage_mode'] == 'json'
    assert rolled['reading_total'] == len(readings)

    status, after = _call('GET', f'/datasets/{dataset_id}', tokens)
    assert after['readings'] == readings
    status, compare = _call('GET', f'/datasets/{dataset_id}/storage/source', tokens)
    assert compare['source']['checksum'] == compare['rows']['checksum']

    # A new trace now reads the rolled-back JSON version with all edits intact.
    status, trace = _call('POST', '/traces', tokens,
                          {'name': f'回退后-{time.time_ns()}', 'plan_id': fresh_dataset['plan_id']})
    status, detail = _call('GET', f"/traces/{trace['id']}", tokens)
    assert detail['snapshot']['storage_mode'] == 'json'
    assert detail['snapshot']['dataset']['readings'] == readings


def test_acceptance_interrupted_backfill_resume_is_idempotent(tokens, fresh_dataset):
    dataset_id = fresh_dataset['id']
    _call('POST', f'/datasets/{dataset_id}/backfill', tokens, {'batch_size': 25})
    _call('POST', f'/datasets/{dataset_id}/backfill/run', tokens, {'batches': 1})
    status, retried = _call('POST', f'/datasets/{dataset_id}/backfill/retry', tokens)
    assert status == 200 and retried['retried_batch'] == {'from': 0, 'to': 25}
    status, again = _call('POST', f'/datasets/{dataset_id}/backfill/retry', tokens)
    assert again['row_count'] == 25  # no duplicate rows from the retry

    status, final = _call('POST', f'/datasets/{dataset_id}/backfill/run', tokens, {'batches': None})
    assert final['status'] == 'completed' and final['row_count'] == 60
    status, compare = _call('GET', f'/datasets/{dataset_id}/storage/source', tokens)
    assert compare['source']['checksum'] == compare['rows']['checksum']
