import os
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3] / 'backend'
sys.path.insert(0, str(BACKEND))

TEST_DATABASE_URL = os.getenv(
    'TEST_DATABASE_URL',
    'postgresql+psycopg://trace@127.0.0.1:17744/operatortrace_test',
)
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ['DATABASE_URL'] = TEST_DATABASE_URL


@pytest.fixture(scope='session', autouse=True)
def _schema():
    from src.config.database import Base, engine
    import src.models.dataset  # noqa: F401
    import src.models.identity  # noqa: F401
    import src.models.plan  # noqa: F401
    import src.models.reading  # noqa: F401
    import src.models.trace  # noqa: F401

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture()
def session():
    from src.config.database import Session
    with Session() as s:
        yield s


@pytest.fixture()
def dataset(session):
    from src.models.dataset import Dataset
    from src.models.plan import Plan
    from src.models.reading import DatasetBackfill, DatasetReading
    from src.models.trace import Trace
    session.query(Trace).delete()
    session.query(Plan).delete()
    session.query(DatasetReading).delete()
    session.query(DatasetBackfill).delete()
    session.query(Dataset).delete()
    session.commit()
    sensors = [
        {'sensor_id': 1, 'zone': '北场', 'device_type': 'pump'},
        {'sensor_id': 2, 'zone': '南场', 'device_type': 'motor'},
    ]
    readings = [
        # duplicate sensor/value/timestamp pairs are intentional
        {'sensor_id': 1, 'value': 80, 'recorded_at': '2026-11-17T09:00:00'},
        {'sensor_id': 1, 'value': 80, 'recorded_at': '2026-11-17T09:00:00'},
        {'sensor_id': 2, 'value': 40, 'recorded_at': '2026-11-17T09:05:00'},
        {'sensor_id': 1, 'value': 55.5, 'recorded_at': '2026-11-17T09:10:00'},
        {'sensor_id': 2, 'value': 51, 'recorded_at': '2026-11-17T09:15:00'},
    ]
    row = Dataset(name='迁移样本', description='测试', sensors=sensors, readings=readings,
                  storage_mode='json', revision=0)
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


@pytest.fixture()
def api(monkeypatch):
    import fakeredis
    from fastapi.testclient import TestClient
    from src.services import traces as trace_service
    from src.utils.security import hash_password

    fake = fakeredis.FakeRedis()
    monkeypatch.setattr(trace_service, 'queue', fake)

    from src.main import app
    from src.config.database import Session
    from src.models.identity import User
    from src.utils.security import token

    with Session() as s:
        if not s.query(User).filter_by(email='operator@test.local').first():
            s.add(User(email='operator@test.local', role='operator', password=hash_password('Trace2026!')))
            s.add(User(email='viewer@test.local', role='viewer', password=hash_password('Trace2026!')))
            s.commit()
        operator = s.query(User).filter_by(email='operator@test.local').one()
        viewer = s.query(User).filter_by(email='viewer@test.local').one()
        operator_token = token(operator)
        viewer_token = token(viewer)

    client = TestClient(app)
    client.headers.update({'Authorization': 'Bearer ' + operator_token})
    client.viewer_headers = {'Authorization': 'Bearer ' + viewer_token}
    client.redis = fake
    return client
