from sqlalchemy import (BigInteger, Column, DateTime, Float, ForeignKey, Integer,
                        String, UniqueConstraint, func)

from src.config.database import Base


class DatasetReading(Base):
    """One sampled reading per row. ``position`` preserves source order.

    Duplicate readings (same sensor/value/timestamp) are intentionally stored as
    separate rows; only ``(dataset_id, position)`` is unique.
    """

    __tablename__ = 'dataset_readings'
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id = Column(Integer, ForeignKey('datasets.id', ondelete='CASCADE'), nullable=False, index=True)
    position = Column(Integer, nullable=False)
    sensor_id = Column(Integer, nullable=False)
    value = Column(Float, nullable=False)
    recorded_at = Column(String(40), nullable=False)
    __table_args__ = (UniqueConstraint('dataset_id', 'position', name='uq_dataset_readings_position'),)


class DatasetBackfill(Base):
    """Resumable copy of ``datasets.readings`` into ``dataset_readings``."""

    __tablename__ = 'dataset_backfills'
    id = Column(Integer, primary_key=True)
    dataset_id = Column(Integer, ForeignKey('datasets.id', ondelete='CASCADE'), nullable=False, index=True)
    status = Column(String(20), nullable=False, default='pending', server_default='pending')
    batch_size = Column(Integer, nullable=False, default=200, server_default='200')
    cursor = Column(Integer, nullable=False, default=0, server_default='0')
    source_total = Column(Integer, nullable=False, default=0, server_default='0')
    copied_rows = Column(Integer, nullable=False, default=0, server_default='0')
    last_batch_size = Column(Integer, nullable=False, default=0, server_default='0')
    batches_run = Column(Integer, nullable=False, default=0, server_default='0')
    checksum = Column(String(64), nullable=True)
    error = Column(String(1000), nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
