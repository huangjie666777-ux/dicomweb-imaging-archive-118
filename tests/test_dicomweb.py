from __future__ import annotations

import threading

from fastapi.testclient import TestClient
from pydicom import dcmread
from pydicom.dataset import Dataset
from pydicom.uid import ImplicitVRLittleEndian

from app.config import Settings
from app.http import create_app
from conftest import multipart


def _uids(client):
    study = client.get("/studies?PatientID=PID-1").json()[0]
    study_uid = study["0020000D"]["Value"][0]
    instance = client.get(f"/studies/{study_uid}/instances").json()[0]
    return (
        study_uid,
        instance["0020000E"]["Value"][0],
        instance["00080018"]["Value"][0],
    )


def test_store_valid_instances_and_qido(workspace):
    client, samples, _data, _settings = workspace
    body, headers = multipart(sorted(samples.glob("*.dcm")))
    response = client.post("/studies", content=body, headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["00081199"]["Value"]) == 2
    assert payload["00081198"]["Value"] == []

    studies = client.get("/studies?StudyDate=20261003&limit=1&offset=0").json()
    assert len(studies) == 1
    assert studies[0]["00080020"]["vr"] == "DA"
    assert studies[0]["00100020"]["Value"] == ["PID-1"]
    study_uid, series_uid, _sop = _uids(client)

    series = client.get(f"/studies/{study_uid}/series").json()
    assert series[0]["0020000E"]["Value"] == [series_uid]
    assert series[0]["00080060"]["Value"] == ["CT"]
    instances = client.get(f"/studies/{study_uid}/series/{series_uid}/instances").json()
    assert [item["00080018"]["Value"][0] for item in instances] == [
        "1.2.3.10.1.1",
        "1.2.3.10.1.2",
    ]


def test_bad_instance_does_not_block_good_instance(workspace, tmp_path):
    client, samples, _data, _settings = workspace
    truncated = tmp_path / "truncated.dcm"
    truncated.write_bytes((samples / "1.dcm").read_bytes()[:-40])
    body, headers = multipart([truncated, samples / "2.dcm"], boundary="B")
    response = client.post("/studies", content=body, headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["00081199"]["Value"]) == 1
    assert len(payload["00081198"]["Value"]) == 1
    assert payload["00081198"]["Value"][0]["00081197"]["Value"] == [0x0110]


def test_rejects_implicit_vr_and_meta_identity_mismatch(workspace, tmp_path):
    client, samples, _data, _settings = workspace
    implicit = tmp_path / "implicit.dcm"
    ds = dcmread(samples / "1.dcm")
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    ds.save_as(implicit, enforce_file_format=True)
    response = client.post("/studies", content=multipart([implicit])[0], headers=multipart([implicit])[1])
    assert response.status_code == 409
    assert "unsupported TransferSyntaxUID 1.2.840.10008.1.2" in response.text

    mismatched = tmp_path / "mismatch.dcm"
    raw = (samples / "1.dcm").read_bytes()
    mismatched.write_bytes(raw.replace(b"1.2.3.10.1.1", b"1.2.3.10.1.9", 1))
    response = client.post("/studies", content=multipart([mismatched], boundary="C")[0], headers=multipart([mismatched], boundary="C")[1])
    assert response.status_code == 409
    assert "mismatch" in response.text


def test_idempotent_same_bytes_conflict_different_bytes(workspace):
    client, samples, _data, _settings = workspace
    original = samples / "1.dcm"
    body, headers = multipart([original], boundary="D")
    assert client.post("/studies", content=body, headers=headers).status_code == 200
    assert client.post("/studies", content=body, headers=headers).status_code == 200

    ds = dcmread(original)
    ds.PatientID = "PID-CHANGED"
    ds.save_as(original, enforce_file_format=True)
    body2, headers2 = multipart([original], boundary="E")
    conflict = client.post("/studies", content=body2, headers=headers2)
    assert conflict.status_code == 409
    assert "different content" in conflict.text
    assert client.get("/studies?PatientID=PID-1").json()


def test_wado_metadata_bulkdata_and_hierarchy(workspace):
    client, samples, _data, _settings = workspace
    body, headers = multipart(sorted(samples.glob("*.dcm")), boundary="F")
    client.post("/studies", content=body, headers=headers)
    study_uid, series_uid, sop_uid = _uids(client)

    response = client.get(f"/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("multipart/related")
    assert b"DICM" in response.content
    assert response.status_code == 200
    assert client.get(f"/studies/{study_uid}/series/wrong/instances/{sop_uid}").status_code == 404

    metadata = client.get(f"/studies/{study_uid}/metadata").json()
    assert len(metadata) == 2
    uri = metadata[0]["7FE00010"]["BulkDataURI"]
    assert uri.startswith(f"/studies/{study_uid}/series/{series_uid}/instances/")
    pixels = client.get(uri)
    assert pixels.status_code == 200
    assert len(pixels.content) == 8 * 8 * 2


def test_restart_persistence_and_temp_cleanup(workspace, monkeypatch):
    client, samples, data, settings = workspace
    body, headers = multipart(sorted(samples.glob("*.dcm")), boundary="G")
    assert client.post("/studies", content=body, headers=headers).status_code == 200
    study_uid, _, _ = _uids(client)
    (settings.tmp_dir / "stale.part").write_bytes(b"stale")
    client.exit_stack.close()

    restarted = create_app(Settings(data_dir=data, max_request_bytes=10_000_000, max_instances=10))
    with TestClient(restarted) as client2:
        assert client2.get("/studies").json()[0]["0020000D"]["Value"] == [study_uid]
    assert not (settings.tmp_dir / "stale.part").exists()


def test_concurrent_same_uid_is_unique(workspace):
    _client, samples, _data, settings = workspace
    from app.archive import Archive

    archive = Archive(settings)
    errors = []

    def submit():
        import shutil
        target = settings.tmp_dir / f"copy-{threading.get_ident()}.dcm"
        shutil.copyfile(samples / "1.dcm", target)
        try:
            archive.ingest_file(target)
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    [thread.start() for thread in threads]
    [thread.join() for thread in threads]
    assert not errors
    count = archive.conn.execute("SELECT COUNT(*) FROM instances").fetchone()[0]
    assert count == 1


def test_limits(workspace):
    client, samples, data, _settings = workspace
    small_settings = Settings(data_dir=data / "small", max_request_bytes=100, max_instances=10)
    small_app = create_app(small_settings)
    with TestClient(small_app) as small:
        body, headers = multipart([samples / "1.dcm"], boundary="H")
        assert small.post("/studies", content=body, headers=headers).status_code == 413
