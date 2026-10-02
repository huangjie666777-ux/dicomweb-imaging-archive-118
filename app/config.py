"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    max_request_bytes: int
    max_instances: int

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "index.sqlite3"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    data_dir = Path(os.environ.get("DICOMWEB_DATA_DIR", "data")).resolve()
    return Settings(
        data_dir=data_dir,
        max_request_bytes=_env_int("DICOMWEB_MAX_REQUEST_BYTES", 200 * 1024 * 1024),
        max_instances=_env_int("DICOMWEB_MAX_INSTANCES", 1000),
    )

