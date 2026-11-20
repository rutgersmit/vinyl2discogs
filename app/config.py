"""Configuration via environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Discogs
    discogs_token: str = field(default_factory=lambda: _env("DISCOGS_TOKEN"))
    discogs_username: str = field(default_factory=lambda: _env("DISCOGS_USERNAME"))  # empty = resolved via /oauth/identity
    discogs_default_folder_id: int = field(default_factory=lambda: int(_env("DISCOGS_FOLDER_ID", "1")))
    user_agent: str = field(default_factory=lambda: _env("USER_AGENT", "vinyl2discogs/1.0 +https://github.com/rutgersmit/vinyl2discogs"))

    # Vision
    # empty = auto: claude if an API key is set, otherwise ollama
    vision_provider_env: str = field(default_factory=lambda: _env("VISION_PROVIDER").lower())
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-sonnet-5-5"))
    ollama_url: str = field(default_factory=lambda: _env("OLLAMA_URL"))
    ollama_model: str = field(default_factory=lambda: _env("OLLAMA_MODEL", "qwen2.5vl:7b"))
    max_image_px: int = field(default_factory=lambda: int(_env("MAX_IMAGE_PX", "1568")))

    # App
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", "/data")))
    app_password: str = field(default_factory=lambda: _env("APP_PASSWORD"))  # empty = no auth
    worker_concurrency: int = field(default_factory=lambda: int(_env("WORKER_CONCURRENCY", "1")))
    detail_lookups: int = field(default_factory=lambda: int(_env("DETAIL_LOOKUPS", "8")))

    @property
    def vision_provider(self) -> str:
        if self.vision_provider_env:
            return self.vision_provider_env
        return "claude" if self.anthropic_api_key or not self.ollama_url else "ollama"

    @property
    def photos_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "vinyl2discogs.sqlite"


def get_settings() -> Settings:
    return Settings()
