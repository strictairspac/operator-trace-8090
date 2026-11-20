"""Upgrading a database created before row-per-reading storage existed."""
import psycopg
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

LEGACY_URL = 'postgresql+psycopg://trace@127.0.0.1:17744/operatortrace_legacy_test'


@pytest.fixture()
def legacy_engine():
    with psycopg.connect('host=127.0.0.1 port=17744 user=trace dbname=postgres') as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname='operatortrace_legacy_test'")
            if not cur.fetchone():
                cur.execute('CREATE DATABASE operatortrace_legacy_test')
    engine = create_engine(LEGACY_URL)
    with engine.begin() as conn:
        conn.execute(text('DROP TABLE IF EXISTS dataset_readings, dataset_backfills, traces, plans, datasets CASCADE'))
        conn.execute(text("""
            CREATE TABLE datasets (
                id SERIAL PRIMARY KEY,
                name VARCHAR(120) UNIQUE NOT NULL,
                description VARCHAR(500) NOT NULL DEFAULT '',
                sensors JSON NOT NULL,
                readings JSON NOT NULL
            )"""))
        conn.execute(text("""
            INSERT INTO datasets (name, description, sensors, readings) VALUES
            ('旧库样本', '迁移前创建', CAST('[]' AS json), CAST(:readings AS json))"""),
            {'readings': '[{"sensor_id":1,"value":80,"recorded_at":"2026-11-17T09:00:00"},'
                         '{"sensor_id":1,"value":80,"recorded_at":"2026-11-17T09:00:00"}]'})
    yield engine
    engine.dispose()


def test_ensure_schema_upgrades_legacy_database(legacy_engine, monkeypatch):
    import src.config.database as database
    from src.services import storage

    monkeypatch.setattr(database, 'engine', legacy_engine)
    monkeypatch.setattr(database, 'Session', sessionmaker(bind=legacy_engine))
    storage.ensure_schema()
    # Idempotent: a second run is a no-op, not an error.
    storage.ensure_schema()

    columns = {c['name'] for c in inspect(legacy_engine).get_columns('datasets')}
    assert {'storage_mode', 'revision'} <= columns
    tables = set(inspect(legacy_engine).get_table_names())
    assert {'dataset_readings', 'dataset_backfills'} <= tables

    with database.Session() as session:
        row = session.execute(text('SELECT storage_mode, revision, readings FROM datasets')).one()
        assert row.storage_mode == 'json'
        assert row.revision == 0
        assert len(row.readings) == 2

        reader = session.execute(text('SELECT name FROM datasets')).scalar_one()
        assert reader == '旧库样本'
