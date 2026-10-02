"""Coordination between DICOM parsing, blob storage and SQLite index."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from . import dicom
from .config import Settings
from .db import connect, initialize, transaction
from .storage import BlobStore


# STOW-RS failure reason codes (PS3.18 Table 8.7.3-5).
REASON_PROCESSING_FAILURE = 0x0110
REASON_DATA_ELEMENT_NUMBER_MISMATCH = 0x0212
REASON_SOP_CLASS_NOT_SUPPORTED = 0x0122
REASON_OUT_OF_RESOURCES = 0x0213
REASON_CANNOT_UNDERSTAND = 0x0210


class ConflictError(Exception):
    pass


@dataclass
class RetrievedInstance:
    path: Path
    sop_class_uid: str
    sop_uid: str
    metadata: dict


class Archive:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        initialize(settings.db_path)
        self.conn = connect(settings.db_path)
        self.blobs = BlobStore(settings)
        # SQLite writes are serialized at application level to make concurrent
        # same-UID submissions deterministic without lock escalation races.
        self._write_lock = threading.Lock()
        self.blobs.recover(self.conn)

    def close(self) -> None:
        self.conn.close()

    def ingest_file(self, path: Path) -> tuple[bool, str, str, str, int | None]:
        """Persist one independent DICOM instance.

        Returns ``(ok, sop_class_uid, sop_uid, message, failure_reason)``.
        """
        try:
            ds, identity = dicom.load_instance(path)
        except dicom.InstanceError as exc:
            path.unlink(missing_ok=True)
            return False, "", "", str(exc), REASON_PROCESSING_FAILURE

        sha256 = self.blobs.sha256_file(path)
        size = path.stat().st_size

        with self._write_lock:
            try:
                with transaction(self.conn):
                    existing = self.conn.execute(
                        "SELECT * FROM instances WHERE sop_uid = ?", (identity.sop_uid,)
                    ).fetchone()
                    if existing is not None:
                        if existing["blob_sha256"] != sha256:
                            raise ConflictError(
                                "same SOP Instance UID already stored with different content"
                            )
                        if (
                            existing["study_uid"] != identity.study_uid
                            or existing["series_uid"] != identity.series_uid
                            or existing["sop_class_uid"] != identity.sop_class_uid
                        ):
                            raise ConflictError(
                                "same SOP Instance UID already stored in a different hierarchy"
                            )
                        # Identical original bytes: idempotent success.
                        path.unlink(missing_ok=True)
                        return True, identity.sop_class_uid, identity.sop_uid, "already exists", None

                    series = self.conn.execute(
                        "SELECT * FROM series WHERE series_uid = ?", (identity.series_uid,)
                    ).fetchone()
                    if series is not None and series["study_uid"] != identity.study_uid:
                        raise ConflictError(
                            "SeriesInstanceUID already belongs to another StudyInstanceUID"
                        )

                    study_meta = json.dumps(dicom.study_json(ds), separators=(",", ":"))
                    series_meta = json.dumps(dicom.series_json(ds), separators=(",", ":"))
                    instance_meta = json.dumps(
                        dicom.instance_json(ds), separators=(",", ":")
                    )

                    self._upsert_study(identity, study_meta)
                    if series is None:
                        self.conn.execute(
                            """INSERT INTO series (series_uid, study_uid, modality,
                                   series_number, metadata_json)
                               VALUES (?, ?, ?, ?, ?)""",
                            (
                                identity.series_uid,
                                identity.study_uid,
                                identity.modality,
                                identity.series_number,
                                series_meta,
                            ),
                        )

                    blob_target = self.blobs.blob_path(sha256)
                    blob_existed = blob_target.is_file()
                    if not blob_existed:
                        # Move through the same tmp/objects subtree; publish after
                        # all validation and immediately before index commit.
                        self.blobs.commit_staged(path, sha256)
                    else:
                        path.unlink(missing_ok=True)

                    try:
                        self.conn.execute(
                            """INSERT INTO instances (sop_uid, series_uid, study_uid,
                                   sop_class_uid, instance_no, blob_sha256, blob_size,
                                   metadata_json)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                identity.sop_uid,
                                identity.series_uid,
                                identity.study_uid,
                                identity.sop_class_uid,
                                identity.instance_number,
                                sha256,
                                size,
                                instance_meta,
                            ),
                        )
                    except Exception:
                        if not blob_existed and blob_target.exists():
                            try:
                                blob_target.unlink()
                            except OSError:
                                pass
                        raise
            except ConflictError as exc:
                path.unlink(missing_ok=True)
                return (
                    False,
                    identity.sop_class_uid,
                    identity.sop_uid,
                    str(exc),
                    REASON_DATA_ELEMENT_NUMBER_MISMATCH,
                )
            except (OSError, sqlite3.Error) as exc:
                path.unlink(missing_ok=True)
                return False, identity.sop_class_uid, identity.sop_uid, str(exc), REASON_OUT_OF_RESOURCES
        return True, identity.sop_class_uid, identity.sop_uid, "stored", None

    def _upsert_study(self, identity: dicom.InstanceIdentity, metadata: str) -> None:
        existing = self.conn.execute(
            "SELECT study_uid FROM studies WHERE study_uid = ?", (identity.study_uid,)
        ).fetchone()
        if existing is None:
            self.conn.execute(
                """INSERT INTO studies (study_uid, patient_id, patient_name, study_date,
                       study_id, metadata_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    identity.study_uid,
                    identity.patient_id,
                    identity.patient_name,
                    identity.study_date,
                    identity.study_id,
                    metadata,
                ),
            )

    def query_studies(self, filters: dict[str, str], limit: int, offset: int) -> list[dict]:
        where, params = _study_where(filters)
        rows = self.conn.execute(
            f"""SELECT s.*, COUNT(DISTINCT se.series_uid) AS series_count,
                      COUNT(i.sop_uid) AS instance_count
               FROM studies s
               LEFT JOIN series se ON se.study_uid = s.study_uid
               LEFT JOIN instances i ON i.study_uid = s.study_uid
               {where}
               GROUP BY s.study_uid
               ORDER BY s.study_uid ASC
               LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return [_row_json_with_counts(row, "00201206", "00201208") for row in rows]

    def query_series(self, filters: dict[str, str], limit: int, offset: int) -> list[dict]:
        where, params = _series_where(filters)
        rows = self.conn.execute(
            f"""SELECT se.*, COUNT(i.sop_uid) AS instance_count
               FROM series se JOIN instances i ON i.series_uid = se.series_uid
               {where}
               GROUP BY se.series_uid
               ORDER BY se.series_uid ASC
               LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["metadata_json"])
            item["00201209"] = {"vr": "IS", "Value": [row["instance_count"]]}
            result.append(item)
        return result

    def query_instances(self, filters: dict[str, str], limit: int, offset: int) -> list[dict]:
        where, params = _instance_where(filters)
        rows = self.conn.execute(
            f"""SELECT i.*, se.study_uid AS f_study_uid
               FROM instances i JOIN series se ON se.series_uid = i.series_uid
               {where}
               ORDER BY i.sop_uid ASC
               LIMIT ? OFFSET ?""",
            params + [limit, offset],
        ).fetchall()
        return [json.loads(row["metadata_json"]) for row in rows]

    def retrieve_instances(
        self, study_uid: str, series_uid: str | None = None, sop_uid: str | None = None
    ) -> list[RetrievedInstance]:
        clauses = ["i.study_uid = ?"]
        params = [study_uid]
        if series_uid is not None:
            clauses.append("i.series_uid = ?")
            params.append(series_uid)
        if sop_uid is not None:
            clauses.append("i.sop_uid = ?")
            params.append(sop_uid)
        rows = self.conn.execute(
            f"""SELECT i.* FROM instances i WHERE {' AND '.join(clauses)}
               ORDER BY i.series_uid ASC, i.sop_uid ASC""",
            params,
        ).fetchall()
        if not rows:
            return []
        instances = []
        for row in rows:
            path = self.blobs.blob_path(row["blob_sha256"])
            if not path.is_file():
                raise FileNotFoundError(f"stored object missing for SOP {row['sop_uid']}")
            instances.append(
                RetrievedInstance(
                    path=path,
                    sop_class_uid=row["sop_class_uid"],
                    sop_uid=row["sop_uid"],
                    metadata=json.loads(row["metadata_json"]),
                )
            )
        return instances

    def retrieve_metadata(
        self, study_uid: str, series_uid: str | None = None, sop_uid: str | None = None
    ) -> list[dict]:
        retrieved = self.retrieve_instances(study_uid, series_uid, sop_uid)
        metadata = []
        for item in retrieved:
            ds, _identity = dicom.load_instance(item.path)
            uri = (
                f"/studies/{_identity.study_uid}/series/{_identity.series_uid}"
                f"/instances/{item.sop_uid}/bulkdata/7FE00010"
            )
            metadata.append(dicom.full_metadata_json(ds, uri))
        return metadata

    def pixel_data(
        self, study_uid: str, series_uid: str, sop_uid: str
    ) -> tuple[bytes, str]:
        rows = self.retrieve_instances(study_uid, series_uid, sop_uid)
        if not rows:
            return b"", ""
        ds, identity = dicom.load_instance(rows[0].path)
        from pydicom.tag import Tag

        if identity.study_uid != study_uid or identity.series_uid != series_uid:
            return b"", ""
        if Tag(0x7FE00010) not in ds:
            raise KeyError("PixelData")
        return bytes(ds.PixelData), identity.sop_class_uid


def _row_json_with_counts(row: sqlite3.Row, series_tag: str, instance_tag: str) -> dict:
    item = json.loads(row["metadata_json"])
    item[series_tag] = {"vr": "IS", "Value": [row["series_count"]]}
    item[instance_tag] = {"vr": "IS", "Value": [row["instance_count"]]}
    return item


def _study_where(filters: dict[str, str]) -> tuple[str, list[str]]:
    clauses = []
    params: list[str] = []
    if "StudyInstanceUID" in filters:
        clauses.append("s.study_uid = ?")
        params.append(filters["StudyInstanceUID"])
    if "PatientID" in filters:
        clauses.append("s.patient_id = ?")
        params.append(filters["PatientID"])
    if "StudyDate" in filters:
        clauses.append("s.study_date = ?")
        params.append(filters["StudyDate"])
    return (("WHERE " + " AND ".join(clauses)) if clauses else ""), params


def _series_where(filters: dict[str, str]) -> tuple[str, list[str]]:
    clauses = []
    params: list[str] = []
    if "StudyInstanceUID" in filters:
        clauses.append("se.study_uid = ?")
        params.append(filters["StudyInstanceUID"])
    if "SeriesInstanceUID" in filters:
        clauses.append("se.series_uid = ?")
        params.append(filters["SeriesInstanceUID"])
    return (("WHERE " + " AND ".join(clauses)) if clauses else ""), params


def _instance_where(filters: dict[str, str]) -> tuple[str, list[str]]:
    clauses = []
    params: list[str] = []
    if "StudyInstanceUID" in filters:
        clauses.append("i.study_uid = ?")
        params.append(filters["StudyInstanceUID"])
    if "SeriesInstanceUID" in filters:
        clauses.append("i.series_uid = ?")
        params.append(filters["SeriesInstanceUID"])
    if "SOPInstanceUID" in filters:
        clauses.append("i.sop_uid = ?")
        params.append(filters["SOPInstanceUID"])
    return (("WHERE " + " AND ".join(clauses)) if clauses else ""), params
