from __future__ import annotations

import importlib
import json
import sys
import uuid
from io import BytesIO
from pathlib import Path

import pytest
from pydicom import dcmread, dcmwrite
from pydicom.uid import ImplicitVRLittleEndian

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from make_sample import build_instance  # noqa: E402
from pydicom.uid import generate_uid  # noqa: E402


def make_bytes(**overrides) -> bytes:
    study_uid = overrides.pop("study_uid", generate_uid())
    series_uid = overrides.pop("series_uid", generate_uid())
    sop_uid = overrides.pop("sop_uid", generate_uid())
    ds = build_instance(
        study_uid, series_uid, sop_uid,
        overrides.pop("patient_id", "PAT-1"),
        overrides.pop("study_date", "20240101"),
        overrides.pop("series_number", 1),
        overrides.pop("instance_number", 1),
        overrides.pop("pattern", 3),
    )
    for key, value in overrides.items():
        setattr(ds, key, value)
    buffer = BytesIO()
    dcmwrite(buffer, ds, enforce_file_format=True)
    return buffer.getvalue()


def stow_body(parts) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    body = bytearray()
    for payload in parts:
        body += f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode()
        body += payload
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), boundary


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DICOMWEB_DATA_DIR", str(tmp_path / "data"))
    for module in ("app.config", "app.store", "app.stow", "app.qido", "app.wado", "app.main"):
        if module in sys.modules:
            importlib.reload(sys.modules[module])
        else:
            importlib.import_module(module)
    from app.main import app
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


def stow(client, parts):
    body, boundary = stow_body(parts)
    return client.post(
        "/studies",
        content=body,
        headers={"Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}'},
    )


def test_store_query_retrieve_roundtrip(client):
    study_uid, series_uid = generate_uid(), generate_uid()
    one = make_bytes(study_uid=study_uid, series_uid=series_uid, instance_number=1)
    two = make_bytes(study_uid=study_uid, series_uid=series_uid, instance_number=2)
    resp = stow(client, [one, two])
    assert resp.status_code == 200
    assert len(resp.json()["00081199"]["Value"]) == 2

    studies = client.get("/studies").json()
    assert len(studies) == 1
    assert studies[0]["0020000D"]["Value"] == [study_uid]
    assert studies[0]["00100020"]["Value"] == ["PAT-1"]

    series = client.get(f"/studies/{study_uid}/series").json()
    assert len(series) == 1
    assert series[0]["0020000E"]["Value"] == [series_uid]

    instances = client.get(
        f"/studies/{study_uid}/series/{series_uid}/instances").json()
    assert len(instances) == 2

    filtered = client.get("/studies", params={"PatientID": "PAT-1"}).json()
    assert len(filtered) == 1
    assert client.get("/studies", params={"PatientID": "NOPE"}).json() == []
    by_date = client.get("/studies", params={"StudyDate": "20240101"}).json()
    assert len(by_date) == 1

    resp = client.get(f"/studies/{study_uid}/series/{series_uid}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("multipart/related")
    assert one in resp.content and two in resp.content

    meta = client.get(
        f"/studies/{study_uid}/series/{series_uid}/metadata").json()
    assert len(meta) == 2
    pixel = meta[0]["7FE00010"]
    assert "BulkDataURI" in pixel
    assert f"studies/{study_uid}" in pixel["BulkDataURI"]

    assert client.get(f"/studies/{generate_uid()}").status_code == 404
    assert client.get(
        f"/studies/{study_uid}/series/{generate_uid()}").status_code == 404


def test_idempotent_and_conflict(client):
    raw = make_bytes()
    assert stow(client, [raw]).status_code == 200
    assert stow(client, [raw]).status_code == 200  # same bytes: idempotent
    assert len(client.get("/instances").json()) == 1

    ds = dcmread(BytesIO(raw))
    conflict_uid = ds.SOPInstanceUID
    other = make_bytes(sop_uid=str(conflict_uid))  # same SOP UID, new content
    resp = stow(client, [other])
    assert resp.status_code == 409
    assert len(client.get("/instances").json()) == 1


def test_rejections(client):
    good = make_bytes()
    ds = dcmread(BytesIO(good))
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    buffer = BytesIO()
    dcmwrite(buffer, ds, enforce_file_format=True)
    implicit = buffer.getvalue()

    truncated = good[: len(good) // 2]
    not_dicom = b"hello world" * 20

    resp = stow(client, [implicit, truncated, not_dicom, good])
    assert resp.status_code == 409
    payload = resp.json()
    assert len(payload["00081198"]["Value"]) == 3
    assert len(payload["00081199"]["Value"]) == 1
    assert len(client.get("/instances").json()) == 1


def test_paging(client):
    parts = [make_bytes(instance_number=n) for n in range(1, 4)]
    assert stow(client, parts).status_code == 200
    page1 = client.get("/instances", params={"limit": 2}).json()
    page2 = client.get("/instances", params={"limit": 2, "offset": 2}).json()
    assert len(page1) == 2 and len(page2) == 1
    uids = [i["00080018"]["Value"][0] for i in page1 + page2]
    assert uids == sorted(uids)
