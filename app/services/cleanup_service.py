"""Periodic cleanup of expired uploaded/processed files and their job rows."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.database import SessionLocal
from app.models import Job

logger = logging.getLogger(__name__)


def _unlink_quietly(path_str: str | None) -> None:
    if not path_str:
        return
    path = Path(path_str)
    try:
        if path.exists():
            path.unlink()
    except OSError:
        logger.warning("Could not delete file %s", path, exc_info=True)


def cleanup_expired_jobs() -> int:
    """Delete files + job rows older than the configured retention window.

    Returns the number of jobs cleaned up.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.file_retention_minutes)
    db = SessionLocal()
    removed = 0
    try:
        expired_jobs = db.query(Job).filter(Job.updated_at < cutoff).all()
        for job in expired_jobs:
            _unlink_quietly(job.input_path)
            _unlink_quietly(job.output_path)
            db.delete(job)
            removed += 1
        db.commit()
    finally:
        db.close()
    if removed:
        logger.info("Cleanup removed %d expired job(s)", removed)
    return removed


async def cleanup_loop() -> None:
    """Runs forever, periodically purging expired jobs/files."""
    interval_seconds = settings.cleanup_interval_minutes * 60
    while True:
        try:
            cleanup_expired_jobs()
        except Exception:  # noqa: BLE001 - never let cleanup crash the app
            logger.exception("Cleanup pass failed")
        await asyncio.sleep(interval_seconds)
