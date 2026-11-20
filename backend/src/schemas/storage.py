from pydantic import BaseModel, Field


class BackfillStart(BaseModel):
    batch_size: int = Field(default=200, ge=1, le=2000)


class BackfillRun(BaseModel):
    # ``null`` runs until the copy is complete; each batch commits separately.
    batches: int | None = Field(default=1, ge=1, le=1000)


class BackfillRetry(BaseModel):
    pass
