"""Video processing API: upload+process, job status, download."""
from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Job
from app.schemas import (
    ConcatenateParams,
    JobResponse,
    OperationParams,
    OperationType,
    ProcessVideoResponse,
)
from app.services.job_service import create_job, process_job

logger = logging.getLogger(__name__)
router = APIRouter()

_operation_adapter: TypeAdapter[OperationParams] = TypeAdapter(OperationParams)


def _validate_operation(operation: str, params_json: str) -> OperationParams:
    try:
        raw = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"params must be valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise HTTPException(status_code=422, detail="params must be a JSON object")

    raw["operation"] = operation
    try:
        return _operation_adapter.validate_python(raw)
    except ValidationError as exc:
        errors = [
            {"loc": err["loc"], "msg": err["msg"], "type": err["type"]}
            for err in exc.errors()
        ]
        raise HTTPException(status_code=422, detail=errors) from exc


def _validate_upload(file: UploadFile) -> str:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in settings.allowed_extensions:
        allowed = ", ".join(sorted(settings.allowed_extensions))
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported file extension '{suffix}'. Allowed: {allowed}",
        )
    return suffix


async def _save_upload(file: UploadFile, destination: Path) -> int:
    size = 0
    try:
        with destination.open("wb") as out_file:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_size:
                    raise HTTPException(status_code=413, detail="Uploaded file is too large")
                out_file.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if size == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    return size


@router.post("/process-video", response_model=ProcessVideoResponse)
async def process_video(
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    operation: Annotated[OperationType, Form(description="Editing operation to perform")],
    files: Annotated[
        list[UploadFile], File(description="Source video file(s). Provide 2+ to concatenate.")
    ],
    params: Annotated[str, Form(description="JSON object of operation-specific parameters")] = "{}",
) -> ProcessVideoResponse:
    operation_params = _validate_operation(operation.value, params)

    is_concat = isinstance(operation_params, ConcatenateParams)
    if is_concat and len(files) < 2:
        raise HTTPException(status_code=422, detail="Concatenate requires at least 2 files")
    if not is_concat and len(files) != 1:
        raise HTTPException(
            status_code=422,
            detail="Exactly one file is required for this operation (use 'concatenate' for multiple)",
        )

    job_id = str(uuid.uuid4())
    saved_paths: list[Path] = []
    saved_filenames: list[str] = []
    try:
        for index, upload in enumerate(files):
            suffix = _validate_upload(upload)
            destination = settings.upload_dir / f"{job_id}_{index}{suffix}"
            await _save_upload(upload, destination)
            saved_paths.append(destination)
            saved_filenames.append(upload.filename or destination.name)
    except HTTPException:
        for path in saved_paths:
            path.unlink(missing_ok=True)
        raise

    job = create_job(
        db,
        job_id=job_id,
        operation=operation_params,
        input_filenames=saved_filenames,
        input_paths=saved_paths,
    )

    background_tasks.add_task(process_job, job.id, operation_params)

    return ProcessVideoResponse(job_id=job.id, status=job.status)  # type: ignore[arg-type]


def _job_to_response(job: Job) -> JobResponse:
    download_url = f"/download/{job.output_filename}" if job.output_filename else None
    return JobResponse(
        job_id=job.id,
        operation=job.operation,
        status=job.status,
        progress=job.progress,
        error_message=job.error_message,
        download_url=download_url,
        input_filenames=job.get_input_filenames(),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str, db: Annotated[Session, Depends(get_db)]) -> JobResponse:
    job = db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_to_response(job)


@router.get("/download/{filename}")
async def download_file(filename: str, db: Annotated[Session, Depends(get_db)]) -> FileResponse:
    safe_name = Path(filename).name  # strip any path components to prevent traversal
    if safe_name != filename:
        raise HTTPException(status_code=400, detail="Invalid filename")

    job = db.query(Job).filter(Job.output_filename == safe_name).first()
    if job is None or job.output_path is None:
        raise HTTPException(status_code=404, detail="File not found")

    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="File no longer available")

    input_filenames = job.get_input_filenames()
    base_name = Path(input_filenames[0]).stem if input_filenames else "output"
    return FileResponse(
        path=output_path,
        filename=f"{base_name}_{job.operation}{output_path.suffix}",
        media_type="application/octet-stream",
    )
