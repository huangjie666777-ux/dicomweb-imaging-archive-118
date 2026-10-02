"""Runtime configuration, overridable via environment variables."""
import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("DICOMWEB_DATA_DIR", "data"))
MAX_UPLOAD_BYTES = int(os.environ.get("DICOMWEB_MAX_UPLOAD_BYTES", 200 * 1024 * 1024))
MAX_INSTANCES_PER_REQUEST = int(os.environ.get("DICOMWEB_MAX_INSTANCES", 200))

ARCHIVE_DIR = DATA_DIR / "archive"
TMP_DIR = DATA_DIR / "tmp"
DB_PATH = DATA_DIR / "index.db"
