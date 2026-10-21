# backend/app/config.py
"""Runtime paths and process-level settings.

AI provider settings (URLs, models, API keys) are NOT here: administrators set them in
Settings -> AI Configuration and they are stored in the database (keys encrypted).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # backend/


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file(BASE_DIR / ".env")


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("EXAM_DATA_DIR", BASE_DIR / "data")))
    port: int = int(os.getenv("EXAM_PORT", "8006"))
    max_upload_bytes: int = int(os.getenv("EXAM_MAX_UPLOAD", 40 * 1024 * 1024))
    max_pages: int = int(os.getenv("EXAM_MAX_PAGES", "40"))
    session_hours: int = int(os.getenv("EXAM_SESSION_HOURS", "12"))
    workers: int = int(os.getenv("EXAM_WORKERS", "2"))
    run_jobs: bool = os.getenv("EXAM_RUN_JOBS", "1") == "1"  # tests run jobs inline instead

    @property
    def db_path(self) -> Path:
        return self.data_dir / "exam.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "sheets"

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"


settings = Settings()


def ensure_dirs(s: Settings = settings) -> None:
    s.data_dir.mkdir(parents=True, exist_ok=True)
    s.upload_dir.mkdir(parents=True, exist_ok=True)
