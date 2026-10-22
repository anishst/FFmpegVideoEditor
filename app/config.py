"""Application configuration."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Centralized, environment-overridable settings."""

    app_name: str = "FFmpeg Video Editor"

    storage_dir: Path = BASE_DIR / "storage"
    upload_dir: Path = BASE_DIR / "storage" / "uploads"
    output_dir: Path = BASE_DIR / "storage" / "outputs"

    database_url: str = f"sqlite:///{BASE_DIR / 'storage' / 'jobs.db'}"

    # Maximum upload size in bytes (default 2 GB).
    max_upload_size: int = 2 * 1024 * 1024 * 1024

    # How long (in minutes) processed/uploaded files are kept before cleanup.
    file_retention_minutes: int = 60

    # How often (in minutes) the cleanup background loop runs.
    cleanup_interval_minutes: int = 15

    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"

    allowed_extensions: set[str] = {
        ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".m4v", ".wmv",
    }

    model_config = SettingsConfigDict(env_prefix="VIDEO_EDITOR_", env_file=".env", extra="ignore")


settings = Settings()

# Ensure required directories exist at import time.
settings.storage_dir.mkdir(parents=True, exist_ok=True)
settings.upload_dir.mkdir(parents=True, exist_ok=True)
settings.output_dir.mkdir(parents=True, exist_ok=True)
