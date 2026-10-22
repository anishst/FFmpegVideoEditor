"""SQLAlchemy ORM models."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from app.schemas import JobStatus, OperationType

__all__ = ["Job", "JobStatus", "OperationType"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    operation: Mapped[str] = mapped_column(String(32))
    params: Mapped[str] = mapped_column(Text, default="{}")

    status: Mapped[str] = mapped_column(String(16), default=JobStatus.PENDING.value)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    input_filename: Mapped[str] = mapped_column(String(512))
    input_path: Mapped[str] = mapped_column(String(1024))
    output_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )
