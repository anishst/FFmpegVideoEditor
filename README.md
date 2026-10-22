# FFmpeg Video Editor

A production-ready FastAPI application that uses FFmpeg to edit videos asynchronously, with a built-in web dashboard for interactively uploading files, configuring an editing operation, and tracking job progress in real time.

## Features

- **Web UI** (served by FastAPI + Jinja2 + Tailwind CSS + vanilla JS): drag-and-drop upload, operation picker, dynamic parameter forms, live progress bar, and a download button once a job completes.
- **Editing operations**: Trim, Resize, Extract Audio, Convert Format, Add Watermark, Compress (optionally to a target output size), Concatenate (join 2+ uploaded files in order).
- **Multi-file upload**: drag & drop (or select) multiple files for concatenation, in upload order, with a removable file list in the UI.
- **Asynchronous processing**: uploads return immediately with a `job_id`; FFmpeg runs in the background via `BackgroundTasks` and `asyncio.subprocess`, so the event loop is never blocked.
- **Persistent job tracking**: job state/progress/errors are stored in SQLite via SQLAlchemy, so status survives across requests.
- **Strict validation**: Pydantic models validate operation parameters (discriminated union keyed on `operation`).
- **Automatic cleanup**: a background loop periodically deletes uploaded/processed files (and their job rows) past a configurable retention window.

## Project layout

```
app/
  main.py                  FastAPI app, lifespan (DB init + cleanup loop)
  config.py                Centralized settings (paths, retention, ffmpeg binaries)
  database.py              SQLAlchemy engine/session
  models.py                SQLAlchemy `Job` ORM model
  schemas.py                Pydantic request/response schemas + operation params
  routers/
    pages.py                GET /  (dashboard)
    video.py                 POST /process-video, GET /jobs/{id}, GET /download/{filename}
  services/
    ffmpeg_service.py        Builds & runs FFmpeg commands, parses progress, probes duration
    job_service.py            Orchestrates job creation + background execution
    cleanup_service.py         Periodic retention-based cleanup
  static/
    js/app.js                 Frontend logic (upload, dynamic params, polling)
    css/styles.css
  templates/
    index.html                 Dashboard page
storage/
  uploads/                   Uploaded source videos (gitignored)
  outputs/                    Processed output videos (gitignored)
  jobs.db                     SQLite database (gitignored)
```

## Prerequisites

### 1. Install FFmpeg (and FFprobe)

This app shells out to the `ffmpeg` and `ffprobe` binaries, so they must be installed and available on your `PATH`.

**macOS** (via [Homebrew](https://brew.sh)):
```bash
brew install ffmpeg
```

**Linux (Debian/Ubuntu)**:
```bash
sudo apt-get update && sudo apt-get install -y ffmpeg
```

**Linux (Fedora)**:
```bash
sudo dnf install ffmpeg
```

**Linux (Arch)**:
```bash
sudo pacman -S ffmpeg
```

**Windows**:
- Recommended: install via [winget](https://learn.microsoft.com/windows/package-manager/winget/):
  ```powershell
  winget install --id Gyan.FFmpeg -e
  ```
  Then restart your shell so the updated `PATH` takes effect.
- Alternatively, download a build from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) or [ffmpeg.org](https://ffmpeg.org/download.html#build-windows), extract it, and add the `bin` folder to your `PATH` environment variable manually.

Verify the install:
```bash
ffmpeg -version
ffprobe -version
```

### 2. Install [uv](https://docs.astral.sh/uv/) (Python package/project manager)

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

## Setup

From the project root:

```bash
# Installs Python (per .python-version), creates .venv, installs dependencies from pyproject.toml/uv.lock
uv sync
```

## Running the server

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Then open **http://localhost:8000** in your browser to use the dashboard.

Interactive API docs (Swagger UI) are available at **http://localhost:8000/docs**.

## API reference

| Method | Path                     | Description                                                             |
|--------|--------------------------|---------------------------------------------------------------------------|
| GET    | `/`                      | Serves the web dashboard.                                                 |
| POST   | `/process-video`         | Multipart form upload: one or more `files`, `operation`, `params` (JSON string). Returns `{job_id, status}`. |
| GET    | `/jobs/{job_id}`         | Returns job status, progress %, error message, and download URL.          |
| GET    | `/download/{filename}`   | Downloads a completed job's output file.                                  |

`files` accepts multiple entries with the same field name. Every operation except `concatenate` requires exactly **one** file; `concatenate` requires **two or more**, processed in the order they're uploaded.

### Operations & parameters

| Operation          | Parameters                                                                 |
|---------------------|------------------------------------------------------------------------------|
| `trim`              | `start_time`, `end_time` (e.g. `"00:00:05"`, `"5"`, `"00:01:30.5"`)          |
| `resize`            | `width`, `height`, `maintain_aspect_ratio` (bool)                             |
| `extract_audio`     | `audio_format` (`mp3`, `aac`, `wav`, `flac`, `ogg`)                            |
| `convert_format`    | `target_format` (`mp4`, `mkv`, `avi`, `webm`, `mov`, `gif`)                     |
| `add_watermark`     | `text`, `position` (`top_left`/`top_right`/`bottom_left`/`bottom_right`/`center`), `font_size`, `font_color` |
| `compress`          | `crf` (0–51, lower = higher quality), `preset` (`ultrafast` … `veryslow`), optional `target_size_mb` |
| `concatenate`       | `output_format` (`mp4`, `mkv`, `webm`, `mov`), `crf` (0–51, lower = higher quality), `preset` (`ultrafast` … `veryslow`); requires 2+ files |

For `compress`, if `target_size_mb` is set, `crf` is ignored and the app runs a **two-pass** x264 encode with a bitrate calculated to hit that approximate file size; otherwise it does a single CRF-based pass.

For `concatenate`, every input is scaled/letterboxed to match the first file's resolution and re-encoded (via `filter_complex concat`), so files with different resolutions, codecs, or aspect ratios can still be joined. Each input must contain both video and audio streams.

Example using `curl`:
```bash
# Trim a single file
curl -X POST http://localhost:8000/process-video \
  -F "files=@my_video.mp4" \
  -F "operation=trim" \
  -F 'params={"start_time":"00:00:05","end_time":"00:00:15"}'

# Concatenate multiple files (order = upload order)
curl -X POST http://localhost:8000/process-video \
  -F "files=@clip1.mp4" -F "files=@clip2.mp4" -F "files=@clip3.mp4" \
  -F "operation=concatenate" \
  -F 'params={"output_format":"mp4"}'

# Compress to a target file size instead of a CRF value
curl -X POST http://localhost:8000/process-video \
  -F "files=@my_video.mp4" \
  -F "operation=compress" \
  -F 'params={"target_size_mb":25}'
```

## Configuration

Settings can be overridden via environment variables prefixed with `VIDEO_EDITOR_`, or a `.env` file in the project root. See `app/config.py` for the full list, e.g.:

```bash
VIDEO_EDITOR_FILE_RETENTION_MINUTES=120
VIDEO_EDITOR_CLEANUP_INTERVAL_MINUTES=30
VIDEO_EDITOR_MAX_UPLOAD_SIZE=5368709120   # 5 GB
```

## Notes

- Uploaded and processed files are stored under `storage/` and automatically purged after `VIDEO_EDITOR_FILE_RETENTION_MINUTES` (default: 60 minutes).
- The `add_watermark` operation uses FFmpeg's `drawtext` filter. Since some minimal FFmpeg builds (especially on Windows) ship without fontconfig data, the app automatically falls back to a known system font file per OS; install [DejaVu](https://dejavu-fonts.github.io/) or [Liberation](https://github.com/liberationfonts/liberation-fonts) fonts on minimal Linux images if watermarking fails there.
- FFmpeg/FFprobe stderr output is captured and surfaced (truncated) in the job's `error_message` on failure, and the full output is logged server-side.
