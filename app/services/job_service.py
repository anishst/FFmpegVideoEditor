"""Orchestrates video-processing jobs: DB bookkeeping + FFmpeg execution."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Job
from app.schemas import ConcatenateParams, JobStatus, OperationParams
from app.services.ffmpeg_service import (
    FFmpegError,
    build_command,
    output_extension,
    probe_duration,
    probe_resolution,
    run_ffmpeg,
)

logger = logging.getLogger(__name__)


def create_job(
    db: Session,
    *,
    job_id: str,
    operation: OperationParams,
    input_filenames: list[str],
    input_paths: list[Path],
) -> Job:
    job = Job(
        id=job_id,
        operation=operation.operation.value,
        params=operation.model_dump_json(),
        status=JobStatus.PENDING.value,
        progress=0.0,
        input_filenames=json.dumps(input_filenames),
        input_paths=json.dumps([str(p) for p in input_paths]),
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

        input_paths = [Path(p) for p in job.get_input_paths()]
        extension = output_extension(operation, input_paths[0].suffix)
        output_path = settings.output_dir / f"{job_id}{extension}"
    finally:
        db.close()

    passlog_prefix = settings.output_dir / f"{job_id}_2pass"
    try:
        missing = [p for p in input_paths if not p.exists()]
        if missing:
            raise FFmpegError("One or more source files are missing or were already cleaned up.")

        # For concatenation, report progress against the combined duration of
        # all clips and scale every clip to the first clip's resolution.
        if isinstance(operation, ConcatenateParams):
            duration = 0.0
            for path in input_paths:
                duration += await probe_duration(path)
            resolution = await probe_resolution(input_paths[0])
        else:
            duration = await probe_duration(input_paths[0])
            resolution = None

        commands = build_command(
            operation,
            input_paths,
            output_path,
            duration=duration,
            resolution=resolution,
            passlog_prefix=passlog_prefix,
        )

        async def on_progress(pct: float) -> None:
            await _report_progress(job_id, pct)

        for i, command in enumerate(commands):
            is_last = i == len(commands) - 1
            await run_ffmpeg(command, duration, on_progress=on_progress if is_last else None)

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
    finally:
        for logfile in passlog_prefix.parent.glob(f"{passlog_prefix.name}*"):
            logfile.unlink(missing_ok=True)
