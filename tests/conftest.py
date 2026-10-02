from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import Settings  # noqa: E402
from app.http import create_app  # noqa: E402
from generate_samples import make_instance  # noqa: E402


@pytest.fixture()
def workspace(tmp_path: Path):
    samples = tmp_path / "samples"
    data = tmp_path / "data"
    make_instance(
        samples / "1.dcm",
        study_uid="1.2.3.10",
        series_uid="1.2.3.10.1",
        instance_uid="1.2.3.10.1.1",
        patient_id="PID-1",
        study_date="20261003",
    )
    make_instance(
        samples / "2.dcm",
        study_uid="1.2.3.10",
        series_uid="1.2.3.10.1",
        instance_uid="1.2.3.10.1.2",
        patient_id="PID-1",
        study_date="20261003",
    )
    settings = Settings(data_dir=data, max_request_bytes=10_000_000, max_instances=10)
    app = create_app(settings)
    with TestClient(app) as client:
        yield client, samples, data, settings


def multipart(files: list[Path], boundary: str = "TESTBOUNDARY") -> tuple[bytes, dict[str, str]]:
    body = f"--{boundary}\r\n".encode()
    for path in files:
        body += b"Content-Type: application/dicom\r\n\r\n" + path.read_bytes() + b"\r\n"
        body += f"--{boundary}\r\n".encode()
    body = body[:-2] + b"--\r\n"
    return body, {
        "Content-Type": f"multipart/related; type=\"application/dicom\"; boundary={boundary}"
    }
