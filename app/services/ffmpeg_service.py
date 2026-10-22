"""Wrapper around the FFmpeg/FFprobe CLI tools.

Builds operation-specific command lines, runs FFmpeg as a subprocess,
parses its `-progress` output to report percentage completion, and
surfaces clean error messages when something goes wrong.
"""
from __future__ import annotations

import asyncio
import logging
import platform
import re
import shlex
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path

from app.config import settings
from app.schemas import (
    AddWatermarkParams,
    CompressParams,
    ConvertFormatParams,
    ExtractAudioParams,
    OperationParams,
    ResizeParams,
    TrimParams,
)

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[float], Awaitable[None]]


class FFmpegError(RuntimeError):
    """Raised when FFmpeg/FFprobe fails or the input is invalid."""


def _timestamp_to_seconds(value: str) -> float:
    parts = value.split(":")
    parts = [float(p) for p in parts]
    while len(parts) < 3:
        parts.insert(0, 0.0)
    hours, minutes, seconds = parts
    return hours * 3600 + minutes * 60 + seconds


async def probe_duration(input_path: Path) -> float:
    """Return the media duration (seconds) using ffprobe."""
    cmd = [
        settings.ffprobe_binary,
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(input_path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"ffprobe binary not found ('{settings.ffprobe_binary}'). Is FFmpeg installed and on PATH?"
        ) from exc

    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed to read input file: {stderr.decode(errors='ignore').strip()}")

    try:
        return float(stdout.decode().strip())
    except ValueError:
        return 0.0


def _build_output_path(output_dir: Path, job_id: str, extension: str) -> Path:
    return output_dir / f"{job_id}{extension}"


@lru_cache(maxsize=1)
def _find_default_font() -> str | None:
    """Locate a usable TTF on the host OS.

    Many minimal/static FFmpeg builds (notably on Windows) ship without
    fontconfig data, so the `drawtext` filter needs an explicit `fontfile`
    or it fails with "Cannot load default config file". We probe a handful
    of well-known system font locations as a cross-platform fallback.
    """
    candidates: list[str] = []
    system = platform.system()
    if system == "Windows":
        windir = Path("C:/Windows/Fonts")
        candidates += [str(windir / name) for name in ("arial.ttf", "calibri.ttf", "segoeui.ttf")]
    elif system == "Darwin":
        candidates += [
            "/System/Library/Fonts/Supplemental/Arial.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ]
    else:
        candidates += [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]

    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def build_command(
    operation: OperationParams, input_path: Path, output_path: Path
) -> list[str]:
    """Build the FFmpeg argv list for the requested operation."""
    base = [settings.ffmpeg_binary, "-y", "-i", str(input_path)]

    if isinstance(operation, TrimParams):
        start = _timestamp_to_seconds(operation.start_time)
        end = _timestamp_to_seconds(operation.end_time)
        if end <= start:
            raise FFmpegError("end_time must be greater than start_time")
        return [
            settings.ffmpeg_binary, "-y",
            "-ss", str(start), "-to", str(end),
            "-i", str(input_path),
            "-c", "copy",
            str(output_path),
        ]

    if isinstance(operation, ResizeParams):
        if operation.maintain_aspect_ratio:
            scale = f"{operation.width}:-2"
        else:
            scale = f"{operation.width}:{operation.height}"
        return [*base, "-vf", f"scale={scale}", "-c:a", "copy", str(output_path)]

    if isinstance(operation, ExtractAudioParams):
        codec_map = {"mp3": "libmp3lame", "aac": "aac", "wav": "pcm_s16le", "flac": "flac", "ogg": "libvorbis"}
        return [*base, "-vn", "-acodec", codec_map[operation.audio_format], str(output_path)]

    if isinstance(operation, ConvertFormatParams):
        if operation.target_format == "gif":
            return [*base, "-vf", "fps=10,scale=480:-1:flags=lanczos", str(output_path)]
        return [*base, str(output_path)]

    if isinstance(operation, AddWatermarkParams):
        positions = {
            "top_left": "x=10:y=10",
            "top_right": "x=w-tw-10:y=10",
            "bottom_left": "x=10:y=h-th-10",
            "bottom_right": "x=w-tw-10:y=h-th-10",
            "center": "x=(w-tw)/2:y=(h-th)/2",
        }
        text = operation.text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
        font_clause = ""
        if font_path := _find_default_font():
            escaped_font = font_path.replace("\\", "/").replace(":", "\\:")
            font_clause = f"fontfile='{escaped_font}':"
        drawtext = (
            f"drawtext=text='{text}':{font_clause}fontcolor={operation.font_color}:"
            f"fontsize={operation.font_size}:{positions[operation.position]}:"
            "box=1:boxcolor=black@0.4:boxborderw=5"
        )
        return [*base, "-vf", drawtext, "-codec:a", "copy", str(output_path)]

    if isinstance(operation, CompressParams):
        return [
            *base,
            "-vcodec", "libx264",
            "-crf", str(operation.crf),
            "-preset", operation.preset,
            "-acodec", "aac",
            str(output_path),
        ]

    raise FFmpegError(f"Unsupported operation: {operation!r}")


def output_extension(operation: OperationParams, original_extension: str) -> str:
    if isinstance(operation, ExtractAudioParams):
        return f".{operation.audio_format}"
    if isinstance(operation, ConvertFormatParams):
        return f".{operation.target_format}"
    return original_extension


_TIME_RE = re.compile(r"out_time_ms=(\d+)")
_TIME_SIMPLE_RE = re.compile(r"out_time=(\d+):(\d+):([\d.]+)")


async def run_ffmpeg(
    command: list[str],
    total_duration: float,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Execute an FFmpeg command, reporting progress (0-100) as it runs."""
    progress_cmd = [*command[:-1], "-progress", "pipe:1", "-nostats", command[-1]]

    logger.info("Running FFmpeg: %s", shlex.join(progress_cmd))

    try:
        proc = await asyncio.create_subprocess_exec(
            *progress_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"ffmpeg binary not found ('{settings.ffmpeg_binary}'). Is FFmpeg installed and on PATH?"
        ) from exc

    stderr_lines: list[str] = []

    async def _read_stdout() -> None:
        assert proc.stdout is not None
        async for raw_line in proc.stdout:
            line = raw_line.decode(errors="ignore").strip()
            if total_duration <= 0 or on_progress is None:
                continue
            seconds = None
            if (match := _TIME_RE.search(line)):
                seconds = int(match.group(1)) / 1_000_000
            elif (match := _TIME_SIMPLE_RE.search(line)):
                h, m, s = match.groups()
                seconds = int(h) * 3600 + int(m) * 60 + float(s)
            if seconds is not None:
                pct = max(0.0, min(99.0, (seconds / total_duration) * 100))
                await on_progress(pct)

    async def _read_stderr() -> None:
        assert proc.stderr is not None
        async for raw_line in proc.stderr:
            stderr_lines.append(raw_line.decode(errors="ignore").rstrip())

    await asyncio.gather(_read_stdout(), _read_stderr())
    returncode = await proc.wait()

    if returncode != 0:
        tail = "\n".join(stderr_lines[-20:])
        logger.error("FFmpeg failed (exit %s):\n%s", returncode, tail)
        raise FFmpegError(f"FFmpeg exited with code {returncode}: {tail or 'no error output'}")
