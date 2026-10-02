"""SQLite index + on-disk archive with atomic per-instance commits."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS instances (
    sop_instance_uid TEXT PRIMARY KEY,
    study_instance_uid TEXT NOT NULL,
    series_instance_uid TEXT NOT NULL,
    sop_class_uid TEXT NOT NULL,
    patient_id TEXT,
    study_date TEXT,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    metadata TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_instances_study ON instances(study_instance_uid);
CREATE INDEX IF NOT EXISTS idx_instances_series ON instances(series_instance_uid);
CREATE INDEX IF NOT EXISTS idx_instances_patient ON instances(patient_id);
CREATE INDEX IF NOT EXISTS idx_instances_date ON instances(study_date);
"""


class Conflict(Exception):
    pass


class Store:
    def __init__(self, data_dir: Path | None = None):
        root = Path(data_dir) if data_dir else config.DATA_DIR
        self.archive = root / "archive"
        self.tmp = root / "tmp"
        self.db_path = root / "index.db"
        self.archive.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        # Clean up unpublished temporary files from a previous run.
        for entry in self.tmp.iterdir():
            if entry.is_file():
                entry.unlink()
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(SCHEMA)
        self._db.commit()

    def close(self):
        self._db.close()

    def _rel_path(self, study_uid, series_uid, sop_uid) -> str:
        return str(Path(study_uid) / series_uid / f"{sop_uid}.dcm")

    def commit(self, ds, raw: bytes, metadata: dict) -> str:
        """Atomically publish one instance. Returns 'stored' or 'duplicate'.

        Raises Conflict if the SOP Instance UID exists with different bytes
        (including attempts to move it to another study/series).
        """
        sop_uid = str(ds.SOPInstanceUID)
        digest = hashlib.sha256(raw).hexdigest()
        with self._lock:
            row = self._db.execute(
                "SELECT sha256, study_instance_uid, series_instance_uid FROM instances "
                "WHERE sop_instance_uid = ?", (sop_uid,),
            ).fetchone()
            if row is not None:
                if row["sha256"] == digest:
                    return "duplicate"
                raise Conflict(
                    "SOPInstanceUID already stored with different content; "
                    "instances are never migrated between studies or series"
                )
            rel = self._rel_path(
                str(ds.StudyInstanceUID), str(ds.SeriesInstanceUID), sop_uid
            )
            dest = self.archive / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp_file = self.tmp / f"{digest}.{os.getpid()}.part"
            tmp_file.write_bytes(raw)
            try:
                self._db.execute(
                    "INSERT INTO instances (sop_instance_uid, study_instance_uid,"
                    " series_instance_uid, sop_class_uid, patient_id, study_date,"
                    " sha256, path, metadata) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        sop_uid,
                        str(ds.StudyInstanceUID),
                        str(ds.SeriesInstanceUID),
                        str(ds.SOPClassUID),
                        str(getattr(ds, "PatientID", "") or ""),
                        str(getattr(ds, "StudyDate", "") or ""),
                        digest,
                        rel,
                        json.dumps(metadata),
                    ),
                )
                os.replace(tmp_file, dest)
                self._db.commit()
            except Exception:
                self._db.rollback()
                tmp_file.unlink(missing_ok=True)
                raise
            return "stored"

    def query(self, level: str, filters: dict, limit: int | None, offset: int):
        """Hierarchical QIDO query. filters may include UIDs, PatientID, StudyDate."""
        clauses, params = [], []
        colmap = {
            "StudyInstanceUID": "study_instance_uid",
            "SeriesInstanceUID": "series_instance_uid",
            "SOPInstanceUID": "sop_instance_uid",
            "PatientID": "patient_id",
            "StudyDate": "study_date",
        }
        for key, col in colmap.items():
            if filters.get(key):
                clauses.append(f"{col} = ?")
                params.append(filters[key])
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        order = {
            "study": "study_instance_uid",
            "series": "series_instance_uid",
            "instance": "sop_instance_uid",
        }[level]
        sql = f"SELECT * FROM instances{where} ORDER BY {order}, sop_instance_uid"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
            if offset:
                sql += " OFFSET ?"
                params.append(offset)
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        rows = self._db.execute(sql, params).fetchall()
        if level == "study":
            seen, out = {}, []
            for r in rows:
                seen.setdefault(r["study_instance_uid"], r)
            return list(seen.values())
        if level == "series":
            seen, out = {}, []
            for r in rows:
                seen.setdefault((r["study_instance_uid"], r["series_instance_uid"]), r)
            return list(seen.values())
        return rows

    def get_instances(self, study_uid, series_uid=None, sop_uid=None):
        sql = "SELECT * FROM instances WHERE study_instance_uid = ?"
        params = [study_uid]
        if series_uid is not None:
            sql += " AND series_instance_uid = ?"
            params.append(series_uid)
        if sop_uid is not None:
            sql += " AND sop_instance_uid = ?"
            params.append(sop_uid)
        sql += " ORDER BY sop_instance_uid"
        return self._db.execute(sql, params).fetchall()

    def read_bytes(self, row) -> bytes:
        return (self.archive / row["path"]).read_bytes()
