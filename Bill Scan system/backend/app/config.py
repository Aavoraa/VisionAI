# backend/app/config.py
"""Runtime settings, read once from the environment."""

import os
from dataclasses import dataclass
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


def _load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines from backend/.env; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    model: str = os.getenv("BILLS_MODEL", "claude-opus-5")
    effort: str = os.getenv("BILLS_EFFORT", "medium")
    # Which reader extracts the bill: "vision_api" (our local Vision AI
    # service, needs VISION_API_KEY) or "claude" (needs ANTHROPIC_API_KEY).
    extractor: str = os.getenv("BILLS_EXTRACTOR", "vision_api").strip().lower()
    vision_api_url: str = os.getenv("VISION_API_URL", "http://127.0.0.1:8002")
    vision_api_key: str = os.getenv("VISION_API_KEY", "").strip()
    vision_api_timeout: float = float(os.getenv("VISION_API_TIMEOUT", "180"))
    data_dir: Path = BASE_DIR / "data"
    upload_dir: Path = BASE_DIR / "data" / "uploads"
    db_path: Path = BASE_DIR / "data" / "daily_bills.db"
    max_upload_bytes: int = int(os.getenv("BILLS_MAX_UPLOAD", 15 * 1024 * 1024))
    # Comma-separated origins the SPA is served from during development.
    cors_origins: str = os.getenv(
        "BILLS_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
    )

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"

    @property
    def origins(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


settings = Settings()
settings.upload_dir.mkdir(parents=True, exist_ok=True)
