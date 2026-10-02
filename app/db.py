"""SQLite index for studies, series and instances."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
    study_uid      TEXT PRIMARY KEY,
    patient_id     TEXT,
    patient_name   TEXT,
    study_date     TEXT,
    study_id       TEXT,
    metadata_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
    series_uid     TEXT PRIMARY KEY,
    study_uid      TEXT NOT NULL REFERENCES studies(study_uid),
    modality       TEXT,
    series_number  INTEGER,
    metadata_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS instances (
    sop_uid        TEXT PRIMARY KEY,
    series_uid     TEXT NOT NULL REFERENCES series(series_uid),
    study_uid      TEXT NOT NULL REFERENCES studies(study_uid),
    sop_class_uid  TEXT NOT NULL,
    instance_no    INTEGER,
    blob_sha256    TEXT NOT NULL,
    blob_size      INTEGER NOT NULL,
    metadata_json  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_series_study ON series(study_uid);
CREATE INDEX IF NOT EXISTS idx_instances_series ON instances(series_uid);
CREATE INDEX IF NOT EXISTS idx_instances_study ON instances(study_uid);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path), timeout=30.0, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def initialize(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager

def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
