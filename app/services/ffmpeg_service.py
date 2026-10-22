"""Wrapper around the FFmpeg/FFprobe CLI tools.

Builds operation-specific command lines, runs FFmpeg as a subprocess,
parses its `-progress` output to report percentage completion, and
surfaces clean error messages when something goes wrong.
"""
from __future__ import annotations

import asyncio
import logging
import os
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
    ConcatenateParams,
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


async def probe_resolution(input_path: Path) -> tuple[int, int]:
    """Return the (width, height) of the first video stream using ffprobe."""
    cmd = [
        settings.ffprobe_binary,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
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
        width_str, height_str = stdout.decode().strip().split(",")[:2]
        return int(width_str), int(height_str)
    except (ValueError, IndexError) as exc:
        raise FFmpegError("Could not determine video resolution") from exc


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


_AUDIO_BITRATE_KBPS = 128

_METADATA_CONTAINER_EXTENSIONS = {".mp4", ".mov", ".m4v"}


def _metadata_args(output_path: Path) -> list[str]:
    """Best-effort flags to retain EXIF-style metadata (creation time, GPS
    location, camera make/model, custom tags, ...) from the first input
    file on the output.

    `-map_metadata 0` is required explicitly: FFmpeg does *not* reliably
    carry over global metadata like `creation_time` on its own, even for a
    plain stream copy. `use_metadata_tags` additionally tells the MOV/MP4
    muxer to keep arbitrary/custom tags instead of silently dropping any
    tag it doesn't specifically recognize.
    """
    args = ["-map_metadata", "0"]
    if output_path.suffix.lower() in _METADATA_CONTAINER_EXTENSIONS:
        args += ["-movflags", "use_metadata_tags"]
    return args


def _concat_metadata_args(output_path: Path) -> list[str]:
    """Metadata mapping for concatenation.

    The filtergraph always produces exactly one video + one audio output
    stream (`[outv]`/`[outa]`), so it's safe to also pull per-stream
    metadata (e.g. the `rotate` tag) from the first clip's first streams,
    on top of the usual global-metadata handling.
    """
    return [
        *_metadata_args(output_path),
        "-map_metadata:s:v:0", "0:s:v:0",
        "-map_metadata:s:a:0", "0:s:a:0",
    ]


def build_command(
    operation: OperationParams,
    input_paths: list[Path],
    output_path: Path,
    *,
    duration: float = 0.0,
    resolution: tuple[int, int] | None = None,
    passlog_prefix: Path | None = None,
) -> list[list[str]]:
    """Build the FFmpeg argv list(s) for the requested operation.

    Returns a list of commands to run *in order* (most operations need
    only one pass; size-targeted compression needs two).
    """
    input_path = input_paths[0]
    base = [settings.ffmpeg_binary, "-y", "-i", str(input_path)]

    if isinstance(operation, TrimParams):
        start = _timestamp_to_seconds(operation.start_time)
        end = _timestamp_to_seconds(operation.end_time)
        if end <= start:
            raise FFmpegError("end_time must be greater than start_time")
        return [[
            settings.ffmpeg_binary, "-y",
            "-ss", str(start), "-to", str(end),
            "-i", str(input_path),
            "-c", "copy",
            *_metadata_args(output_path),
            str(output_path),
        ]]

    if isinstance(operation, ResizeParams):
        if operation.maintain_aspect_ratio:
            scale = f"{operation.width}:-2"
        else:
            scale = f"{operation.width}:{operation.height}"
        return [[
            *base, "-vf", f"scale={scale}", "-c:a", "copy",
            *_metadata_args(output_path),
            str(output_path),
        ]]

    if isinstance(operation, ExtractAudioParams):
        codec_map = {"mp3": "libmp3lame", "aac": "aac", "wav": "pcm_s16le", "flac": "flac", "ogg": "libvorbis"}
        return [[
            *base, "-vn", "-acodec", codec_map[operation.audio_format],
            *_metadata_args(output_path),
            str(output_path),
        ]]

    if isinstance(operation, ConvertFormatParams):
        if operation.target_format == "gif":
            # GIF has no metadata container, so there is nothing to retain.
            return [[*base, "-vf", "fps=10,scale=480:-1:flags=lanczos", str(output_path)]]
        return [[*base, *_metadata_args(output_path), str(output_path)]]

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
        return [[
            *base, "-vf", drawtext, "-codec:a", "copy",
            *_metadata_args(output_path),
            str(output_path),
        ]]

    if isinstance(operation, CompressParams):
        if operation.target_size_mb:
            if duration <= 0:
                raise FFmpegError("Could not determine input duration; cannot target an output size")
            total_kbits = operation.target_size_mb * 8 * 1024
            video_kbps = max(50.0, (total_kbits / duration) - _AUDIO_BITRATE_KBPS)
            if passlog_prefix is None:
                raise FFmpegError("Missing two-pass log prefix for size-targeted compression")
            null_sink = os.devnull
            pass1 = [
                settings.ffmpeg_binary, "-y", "-i", str(input_path),
                "-c:v", "libx264", "-b:v", f"{video_kbps:.0f}k",
                "-preset", operation.preset,
                "-pass", "1", "-passlogfile", str(passlog_prefix),
                "-an", "-f", "mp4", null_sink,
            ]
            pass2 = [
                settings.ffmpeg_binary, "-y", "-i", str(input_path),
                "-c:v", "libx264", "-b:v", f"{video_kbps:.0f}k",
                "-preset", operation.preset,
                "-pass", "2", "-passlogfile", str(passlog_prefix),
                "-c:a", "aac", "-b:a", f"{_AUDIO_BITRATE_KBPS}k",
                *_metadata_args(output_path),
                str(output_path),
            ]
            return [pass1, pass2]

        return [[
            *base,
            "-vcodec", "libx264",
            "-crf", str(operation.crf),
            "-preset", operation.preset,
            "-acodec", "aac",
            *_metadata_args(output_path),
            str(output_path),
        ]]

    if isinstance(operation, ConcatenateParams):
        if len(input_paths) < 2:
            raise FFmpegError("Concatenation requires at least 2 input files")
        width, height = resolution or (1280, 720)

        inputs_args: list[str] = []
        scale_filters: list[str] = []
        concat_refs: list[str] = []
        for idx, path in enumerate(input_paths):
            inputs_args += ["-i", str(path)]
            scale_filters.append(
                f"[{idx}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1[v{idx}]"
            )
            concat_refs.append(f"[v{idx}][{idx}:a]")

        filter_complex = ";".join(scale_filters)
        filter_complex += ";" + "".join(concat_refs)
        filter_complex += f"concat=n={len(input_paths)}:v=1:a=1[outv][outa]"

        return [[
            settings.ffmpeg_binary, "-y",
            *inputs_args,
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            "-c:v", "libx264", "-crf", str(operation.crf), "-preset", operation.preset,
            "-c:a", "aac",
            *_concat_metadata_args(output_path),
            str(output_path),
        ]]

    raise FFmpegError(f"Unsupported operation: {operation!r}")


def output_extension(operation: OperationParams, original_extension: str) -> str:
    if isinstance(operation, ExtractAudioParams):
        return f".{operation.audio_format}"
    if isinstance(operation, ConvertFormatParams):
        return f".{operation.target_format}"
    if isinstance(operation, ConcatenateParams):
        return f".{operation.output_format}"
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
