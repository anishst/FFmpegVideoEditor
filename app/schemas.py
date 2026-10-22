"""Pydantic schemas: request/response validation."""
from __future__ import annotations

import enum
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class OperationType(str, enum.Enum):
    TRIM = "trim"
    RESIZE = "resize"
    EXTRACT_AUDIO = "extract_audio"
    CONVERT_FORMAT = "convert_format"
    ADD_WATERMARK = "add_watermark"
    COMPRESS = "compress"
    CONCATENATE = "concatenate"


_TIMESTAMP_PATTERN = r"^\d{1,2}(:\d{2}){0,2}(\.\d{1,3})?$"


class TrimParams(BaseModel):
    operation: Literal[OperationType.TRIM] = OperationType.TRIM
    start_time: str = Field(..., description="Start timestamp, e.g. 00:00:05 or 5")
    end_time: str = Field(..., description="End timestamp, e.g. 00:00:15 or 15")

    @field_validator("start_time", "end_time")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        import re

        if not re.match(_TIMESTAMP_PATTERN, value.strip()):
            raise ValueError("Timestamps must look like SS, MM:SS, or HH:MM:SS(.ms)")
        return value.strip()


class ResizeParams(BaseModel):
    operation: Literal[OperationType.RESIZE] = OperationType.RESIZE
    width: int = Field(..., gt=0, le=7680)
    height: int = Field(..., gt=0, le=4320)
    maintain_aspect_ratio: bool = True


class ExtractAudioParams(BaseModel):
    operation: Literal[OperationType.EXTRACT_AUDIO] = OperationType.EXTRACT_AUDIO
    audio_format: Literal["mp3", "aac", "wav", "flac", "ogg"] = "mp3"


class ConvertFormatParams(BaseModel):
    operation: Literal[OperationType.CONVERT_FORMAT] = OperationType.CONVERT_FORMAT
    target_format: Literal["mp4", "mkv", "avi", "webm", "mov", "gif"] = "mp4"


class AddWatermarkParams(BaseModel):
    operation: Literal[OperationType.ADD_WATERMARK] = OperationType.ADD_WATERMARK
    text: str = Field(..., min_length=1, max_length=120)
    position: Literal[
        "top_left", "top_right", "bottom_left", "bottom_right", "center"
    ] = "bottom_right"
    font_size: int = Field(24, ge=8, le=200)
    font_color: str = Field("white", max_length=32)


class CompressParams(BaseModel):
    operation: Literal[OperationType.COMPRESS] = OperationType.COMPRESS
    crf: int = Field(28, ge=0, le=51, description="Constant Rate Factor; higher = smaller/worse")
    preset: Literal[
        "ultrafast", "superfast", "veryfast", "faster", "fast",
        "medium", "slow", "slower", "veryslow",
    ] = "medium"
    target_size_mb: float | None = Field(
        None,
        gt=0,
        le=100_000,
        description=(
            "If set, ignores crf and uses two-pass encoding to hit this "
            "approximate output size (in megabytes) instead."
        ),
    )


class ConcatenateParams(BaseModel):
    operation: Literal[OperationType.CONCATENATE] = OperationType.CONCATENATE
    output_format: Literal["mp4", "mkv", "webm", "mov"] = "mp4"


OperationParams = Annotated[
    Union[
        TrimParams,
        ResizeParams,
        ExtractAudioParams,
        ConvertFormatParams,
        AddWatermarkParams,
        CompressParams,
        ConcatenateParams,
    ],
    Field(discriminator="operation"),
]


class ProcessVideoResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    job_id: str
    operation: OperationType
    status: JobStatus
    progress: float
    error_message: str | None = None
    download_url: str | None = None
    input_filenames: list[str] = []
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
