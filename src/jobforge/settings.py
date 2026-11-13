"""Runtime configuration loaded from environment variables.

The desktop app stays safe-by-default: it binds to loopback and API-key
authentication is disabled unless JOBFORGE_API_KEY is explicitly set.
"""
import os
from dataclasses import dataclass
from typing import Tuple


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be between 1 and 65535")
    return value


@dataclass(frozen=True)
class Settings:
    environment: str
    service_name: str
    host: str
    port: int
    api_key: str
    log_level: str
    log_format: str
    cors_origins: Tuple[str, ...]

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_key)


def get_settings() -> Settings:
    origins_raw = (os.environ.get("JOBFORGE_CORS_ORIGINS") or "*").strip()
    origins = tuple(part.strip() for part in origins_raw.split(",") if part.strip())
    log_format = (os.environ.get("JOBFORGE_LOG_FORMAT") or "json").strip().lower()
    if log_format not in {"json", "text"}:
        raise ValueError("JOBFORGE_LOG_FORMAT must be json or text")

    return Settings(
        environment=(os.environ.get("JOBFORGE_ENV") or "local").strip(),
        service_name=(os.environ.get("JOBFORGE_SERVICE_NAME") or "jobforge").strip(),
        host=(os.environ.get("JOBFORGE_HOST") or DEFAULT_HOST).strip(),
        port=_env_int("JOBFORGE_PORT", DEFAULT_PORT),
        api_key=(os.environ.get("JOBFORGE_API_KEY") or "").strip(),
        log_level=(os.environ.get("JOBFORGE_LOG_LEVEL") or "INFO").strip().upper(),
        log_format=log_format,
        cors_origins=origins or ("*",),
    )
