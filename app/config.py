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

    # Currency for marketplace prices (Discogs default is USD)
    currency: str = field(default_factory=lambda: _env("DISCOGS_CURRENCY", "EUR").upper())

    # Rare-find alerts: a match is flagged when any signal crosses its threshold (0 = signal off)
    rare_max_have: int = field(default_factory=lambda: int(_env("RARE_MAX_HAVE", "50")))
    rare_min_want_ratio: float = field(default_factory=lambda: float(_env("RARE_MIN_WANT_RATIO", "3")))
    rare_min_want_count: int = field(default_factory=lambda: int(_env("RARE_MIN_WANT_COUNT", "25")))
    rare_min_price: float = field(default_factory=lambda: float(_env("RARE_MIN_PRICE", "100")))
    # Optional: POST a JSON message here on a rare find (Home Assistant webhook, ntfy, Slack-compatible, ...)
    rare_webhook_url: str = field(default_factory=lambda: _env("RARE_WEBHOOK_URL"))

    # Vision
    # empty = auto: claude, then openai (whichever has an API key), otherwise ollama
    vision_provider_env: str = field(default_factory=lambda: _env("VISION_PROVIDER").lower())
    anthropic_api_key: str = field(default_factory=lambda: _env("ANTHROPIC_API_KEY"))
    claude_model: str = field(default_factory=lambda: _env("CLAUDE_MODEL", "claude-sonnet-5-5"))
    openai_api_key: str = field(default_factory=lambda: _env("OPENAI_API_KEY"))
    openai_model: str = field(default_factory=lambda: _env("OPENAI_MODEL", "gpt-6.1-terra"))
    openai_base_url: str = field(default_factory=lambda: _env("OPENAI_BASE_URL", "https://api.openai.com/v1"))
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
        if self.anthropic_api_key:
            return "claude"
        if self.openai_api_key:
            return "openai"
        return "ollama" if self.ollama_url else "claude"

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
