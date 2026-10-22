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
from app.schemas import JobResponse, OperationParams, OperationType, ProcessVideoResponse
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


@router.post("/process-video", response_model=ProcessVideoResponse)
async def process_video(
    background_tasks: BackgroundTasks,
    db: Annotated[Session, Depends(get_db)],
    file: Annotated[UploadFile, File(description="Source video file")],
    operation: Annotated[OperationType, Form(description="Editing operation to perform")],
    params: Annotated[str, Form(description="JSON object of operation-specific parameters")] = "{}",
) -> ProcessVideoResponse:
    suffix = _validate_upload(file)
    operation_params = _validate_operation(operation.value, params)

    job_id = str(uuid.uuid4())
    input_path = settings.upload_dir / f"{job_id}{suffix}"

    size = 0
    try:
        with input_path.open("wb") as out_file:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_upload_size:
                    raise HTTPException(status_code=413, detail="Uploaded file is too large")
                out_file.write(chunk)
    except HTTPException:
        input_path.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    if size == 0:
        input_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    job = create_job(
        db,
        operation=operation_params,
        input_filename=file.filename or input_path.name,
        input_path=input_path,
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

    return FileResponse(
        path=output_path,
        filename=f"{Path(job.input_filename).stem}_{job.operation}{output_path.suffix}",
        media_type="application/octet-stream",
    )
