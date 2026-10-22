"""Orchestrates video-processing jobs: DB bookkeeping + FFmpeg execution."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Job
from app.schemas import JobStatus, OperationParams
from app.services.ffmpeg_service import (
    FFmpegError,
    build_command,
    output_extension,
    probe_duration,
    run_ffmpeg,
)

logger = logging.getLogger(__name__)


def create_job(
    db: Session,
    *,
    operation: OperationParams,
    input_filename: str,
    input_path: Path,
) -> Job:
    job = Job(
        operation=operation.operation.value,
        params=operation.model_dump_json(),
        status=JobStatus.PENDING.value,
        progress=0.0,
        input_filename=input_filename,
        input_path=str(input_path),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _set_progress(job_id: str, progress: float) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is not None:
            job.progress = progress
            db.commit()
    finally:
        db.close()


async def _report_progress(job_id: str, progress: float) -> None:
    _set_progress(job_id, progress)


async def process_job(job_id: str, operation: OperationParams) -> None:
    """Background entry point: runs FFmpeg and keeps the job row updated."""
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            logger.error("Job %s vanished before processing could start", job_id)
            return

        job.status = JobStatus.PROCESSING.value
        job.progress = 0.0
        db.commit()

        input_path = Path(job.input_path)
        extension = output_extension(operation, input_path.suffix)
        output_path = settings.output_dir / f"{job_id}{extension}"
    finally:
        db.close()

    try:
        if not input_path.exists():
            raise FFmpegError("Uploaded source file is missing or was already cleaned up.")

        duration = await probe_duration(input_path)
        command = build_command(operation, input_path, output_path)

        async def on_progress(pct: float) -> None:
            await _report_progress(job_id, pct)

        await run_ffmpeg(command, duration, on_progress=on_progress)

        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if job is not None:
                job.status = JobStatus.COMPLETED.value
                job.progress = 100.0
                job.output_filename = output_path.name
                job.output_path = str(output_path)
                db.commit()
        finally:
            db.close()

    except Exception as exc:  # noqa: BLE001 - surface any failure on the job record
        logger.exception("Job %s failed", job_id)
        db = SessionLocal()
        try:
            job = db.get(Job, job_id)
            if job is not None:
                job.status = JobStatus.FAILED.value
                job.error_message = str(exc)
                db.commit()
        finally:
            db.close()
        if output_path.exists():
            output_path.unlink(missing_ok=True)
