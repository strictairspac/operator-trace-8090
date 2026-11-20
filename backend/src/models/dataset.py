from sqlalchemy import Column, Integer, String, JSON

from src.config.database import Base


class Dataset(Base):
    __tablename__ = 'datasets'
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    description = Column(String(500), nullable=False, default='')
    sensors = Column(JSON, nullable=False, default=list)
    # Legacy JSON source. Kept intact while ``storage_mode='rows'`` so a rollback
    # can be verified against what was switched, and so no record is dropped.
    readings = Column(JSON, nullable=False, default=list)
    storage_mode = Column(String(20), nullable=False, default='json', server_default='json')
    revision = Column(Integer, nullable=False, default=0, server_default='0')
