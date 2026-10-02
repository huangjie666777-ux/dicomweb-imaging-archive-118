"""Content-addressed original-byte object storage and restart recovery."""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import sqlite3
from pathlib import Path

from .config import Settings

logger = logging.getLogger(__name__)


class BlobStore:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _path_for(self, sha256: str) -> Path:
        return self._settings.objects_dir / sha256[:2] / sha256[2:4] / sha256

    def stage_path(self) -> Path:
        # mkstemp is used by callers; this only makes the directory explicit.
        self._settings.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="part-", suffix=".part", dir=self._settings.tmp_dir)
        os.close(fd)
        return Path(name)

    def commit_staged(self, staged_path: Path, sha256: str) -> None:
        target = self._path_for(sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        # os.replace is atomic on POSIX when source and destination share a FS.
        os.replace(staged_path, target)
        self._fsync_with_parent(target)

    def blob_path(self, sha256: str) -> Path:
        return self._path_for(sha256)

    def exists(self, sha256: str) -> bool:
        return self._path_for(sha256).is_file()

    @staticmethod
    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def delete_if_unreferenced(self, conn: sqlite3.Connection, sha256: str) -> None:
        row = conn.execute(
            "SELECT 1 FROM instances WHERE blob_sha256 = ? LIMIT 1", (sha256,)
        ).fetchone()
        if row is None:
            path = self._path_for(sha256)
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    def recover(self, conn: sqlite3.Connection) -> None:
        """Remove unpublished temp files and reconcile blobs against the index."""
        if self._settings.tmp_dir.exists():
            for child in self._settings.tmp_dir.iterdir():
                if child.is_file():
                    child.unlink()
                    logger.info("Removed unpublished temporary file %s", child)
        indexed = {
            row[0]
            for row in conn.execute("SELECT DISTINCT blob_sha256 FROM instances")
        }
        for path in self._settings.objects_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.name not in indexed:
                path.unlink()
                logger.info("Removed orphan blob %s", path)
        for sha in indexed:
            if not self._path_for(sha).is_file():
                logger.error("Indexed blob %s is missing from object storage", sha)
                conn.execute(
                    "DELETE FROM instances WHERE blob_sha256 = ?", (sha,)
                )
        _prune_empty_parents(conn)

    @staticmethod
    def _fsync_with_parent(path: Path) -> None:
        try:
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        except OSError:
            # fsync is a durability best effort on platforms without support.
            pass


def _prune_empty_parents(conn: sqlite3.Connection) -> None:
    conn.execute(
        """DELETE FROM series WHERE NOT EXISTS (
               SELECT 1 FROM instances WHERE instances.series_uid = series.series_uid
           )"""
    )
    conn.execute(
        """DELETE FROM studies WHERE NOT EXISTS (
               SELECT 1 FROM series WHERE series.study_uid = studies.study_uid
           )"""
    )
