"""FastAPI routes implementing a pragmatic subset of DICOMweb."""

from __future__ import annotations

from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import dicom
from .archive import Archive
from .config import Settings, get_settings
from .multipart import (
    InstanceLimitExceeded,
    MultipartError,
    PayloadTooLarge,
    extract_boundary,
    iter_parts,
)


DICOM_JSON = "application/dicom+json"
DICOM = "application/dicom"

KNOWN_STUDY_PARAMS = {"StudyInstanceUID", "PatientID", "StudyDate", "limit", "offset"}
KNOWN_SERIES_PARAMS = {"StudyInstanceUID", "SeriesInstanceUID", "limit", "offset"}
KNOWN_INSTANCE_PARAMS = {"StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "limit", "offset"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()
    archive = Archive(settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            archive.close()

    app = FastAPI(title="Minimal DICOMweb Archive", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.archive = archive

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/studies")
    async def store_studies(request: Request) -> Response:
        try:
            boundary = extract_boundary(request.headers.get("content-type"))
        except MultipartError as exc:
            return JSONResponse(
                {"00081198": {"vr": "US", "Value": [0x0110]}, "error": str(exc)},
                status_code=400,
                media_type=DICOM_JSON,
            )

        referenced: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []
        parts = []
        try:
            async for part in iter_parts(
                request,
                boundary,
                settings.tmp_dir,
                settings.max_request_bytes,
                settings.max_instances,
            ):
                media_type = (
                    part.content_type.split(";", 1)[0].strip().lower()
                    if part.content_type
                    else ""
                )
                if media_type != DICOM:
                    item = dicom.uid_ref_json("", "", 0x0122)
                    item["00081190"] = {
                        "vr": "LO",
                        "Value": [f"part Content-Type must be {DICOM}"[:64]],
                    }
                    failed.append(item)
                    part.cleanup()
                    continue
                parts.append(part)
        except PayloadTooLarge as exc:
            for part in parts:
                part.cleanup()
            return JSONResponse(
                {"00081198": {"vr": "US", "Value": [0x0213]}, "error": str(exc)},
                status_code=413,
                media_type=DICOM_JSON,
            )
        except InstanceLimitExceeded as exc:
            for part in parts:
                part.cleanup()
            return JSONResponse(
                {"00081198": {"vr": "US", "Value": [0x0213]}, "error": str(exc)},
                status_code=413,
                media_type=DICOM_JSON,
            )
        except MultipartError as exc:
            for part in parts:
                part.cleanup()
            return JSONResponse(
                {"00081198": {"vr": "US", "Value": [0x0110]}, "error": str(exc)},
                status_code=400,
                media_type=DICOM_JSON,
            )

        for part in parts:
            # Archive.ingest_file is the owner of the staged path and removes
            # it on expected success/failure paths. One bad part cannot abort
            # processing of later parts.
            try:
                ok, sop_class, sop_uid, message, reason = archive.ingest_file(part.path)
            except Exception as ingest_exc:
                part.cleanup()
                return JSONResponse(
                    {"00081198": {"vr": "US", "Value": [0x0110]}, "error": str(ingest_exc)},
                    status_code=500,
                    media_type=DICOM_JSON,
                )
            if ok:
                referenced.append(dicom.uid_ref_json(sop_class, sop_uid))
            else:
                item = dicom.uid_ref_json(sop_class or "", sop_uid or "", reason or 0x0110)
                item["00081190"] = {"vr": "LO", "Value": [message[:64]]}
                failed.append(item)

        body = {
            "00081199": {"vr": "SQ", "Value": referenced},
            "00081198": {"vr": "SQ", "Value": failed},
        }
        status = 200 if referenced else 409
        return JSONResponse(body, status_code=status, media_type=DICOM_JSON)

    @app.get("/studies")
    def qido_studies(request: Request) -> Response:
        limit, offset, error = _pagination_public(request, KNOWN_STUDY_PARAMS)
        if error:
            return error
        filters = {key: request.query_params[key] for key in KNOWN_STUDY_PARAMS & set(request.query_params)}
        return JSONResponse(archive.query_studies(filters, limit, offset), media_type=DICOM_JSON)

    @app.get("/studies/{study_uid}/series")
    def qido_series(study_uid: str, request: Request) -> Response:
        limit, offset, error = _pagination_public(request, KNOWN_SERIES_PARAMS)
        if error:
            return error
        filters = {key: request.query_params[key] for key in KNOWN_SERIES_PARAMS & set(request.query_params)}
        if "StudyInstanceUID" in filters and filters["StudyInstanceUID"] != study_uid:
            return JSONResponse({"error": "hierarchy mismatch"}, status_code=403)
        filters["StudyInstanceUID"] = study_uid
        return JSONResponse(archive.query_series(filters, limit, offset), media_type=DICOM_JSON)

    @app.get("/series")
    def qido_series_root(request: Request) -> Response:
        limit, offset, error = _pagination_public(request, KNOWN_SERIES_PARAMS)
        if error:
            return error
        filters = {key: request.query_params[key] for key in KNOWN_SERIES_PARAMS & set(request.query_params)}
        return JSONResponse(archive.query_series(filters, limit, offset), media_type=DICOM_JSON)

    @app.get("/studies/{study_uid}/series/{series_uid}/instances")
    def qido_instances_path(study_uid: str, series_uid: str, request: Request) -> Response:
        return _qido_instances(archive, request, study_uid, series_uid)

    @app.get("/instances")
    def qido_instances_root(request: Request) -> Response:
        return _qido_instances(archive, request, None, None)

    @app.get("/studies/{study_uid}/instances")
    def qido_instances_study(study_uid: str, request: Request) -> Response:
        return _qido_instances(archive, request, study_uid, None)

    # WADO-RS retrieval
    @app.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
    def wado_instance(study_uid: str, series_uid: str, sop_uid: str) -> Response:
        instances = archive.retrieve_instances(study_uid, series_uid, sop_uid)
        if not instances:
            return JSONResponse({"error": "instance not found"}, status_code=404, media_type=DICOM_JSON)
        return _multipart_dicom(instances)

    @app.get("/studies/{study_uid}/series/{series_uid}")
    def wado_series(study_uid: str, series_uid: str) -> Response:
        if not archive.retrieve_instances(study_uid, series_uid):
            return JSONResponse({"error": "series not found"}, status_code=404, media_type=DICOM_JSON)
        return _multipart_dicom(archive.retrieve_instances(study_uid, series_uid))

    @app.get("/studies/{study_uid}")
    def wado_study(study_uid: str) -> Response:
        instances = archive.retrieve_instances(study_uid)
        if not instances:
            return JSONResponse({"error": "study not found"}, status_code=404, media_type=DICOM_JSON)
        return _multipart_dicom(instances)

    # DICOM JSON metadata resources
    @app.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata")
    def wado_instance_metadata(study_uid: str, series_uid: str, sop_uid: str) -> Response:
        if not archive.retrieve_instances(study_uid, series_uid, sop_uid):
            return JSONResponse({"error": "instance not found"}, status_code=404, media_type=DICOM_JSON)
        return JSONResponse(archive.retrieve_metadata(study_uid, series_uid, sop_uid), media_type=DICOM_JSON)

    @app.get("/studies/{study_uid}/series/{series_uid}/metadata")
    def wado_series_metadata(study_uid: str, series_uid: str) -> Response:
        if not archive.retrieve_instances(study_uid, series_uid):
            return JSONResponse({"error": "series not found"}, status_code=404, media_type=DICOM_JSON)
        return JSONResponse(archive.retrieve_metadata(study_uid, series_uid), media_type=DICOM_JSON)

    @app.get("/studies/{study_uid}/metadata")
    def wado_study_metadata(study_uid: str) -> Response:
        if not archive.retrieve_instances(study_uid):
            return JSONResponse({"error": "study not found"}, status_code=404, media_type=DICOM_JSON)
        return JSONResponse(archive.retrieve_metadata(study_uid), media_type=DICOM_JSON)

    @app.get(
        "/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/bulkdata/{tag}"
    )
    def bulk_pixel_data(study_uid: str, series_uid: str, sop_uid: str, tag: str) -> Response:
        if tag.upper() != "7FE00010":
            return JSONResponse({"error": "unsupported bulkdata tag"}, status_code=404)
        try:
            data, _sop_class = archive.pixel_data(study_uid, series_uid, sop_uid)
        except KeyError:
            return JSONResponse({"error": "PixelData absent"}, status_code=404)
        if not data:
            return JSONResponse({"error": "instance not found"}, status_code=404)
        return Response(data, media_type="application/octet-stream")

    return app


def _qido_instances(
    archive: Archive, request: Request, fixed_study: str | None, fixed_series: str | None
) -> Response:
    limit, offset, error = _pagination_public(request, KNOWN_INSTANCE_PARAMS)
    if error:
        return error
    filters = {key: request.query_params[key] for key in KNOWN_INSTANCE_PARAMS & set(request.query_params)}
    if fixed_study is not None:
        if "StudyInstanceUID" in filters and filters["StudyInstanceUID"] != fixed_study:
            return JSONResponse({"error": "hierarchy mismatch"}, status_code=403)
        filters["StudyInstanceUID"] = fixed_study
    if fixed_series is not None:
        if "SeriesInstanceUID" in filters and filters["SeriesInstanceUID"] != fixed_series:
            return JSONResponse({"error": "hierarchy mismatch"}, status_code=403)
        filters["SeriesInstanceUID"] = fixed_series
    return JSONResponse(archive.query_instances(filters, limit, offset), media_type=DICOM_JSON)


def _pagination_public(request: Request, known: set[str]):
    unknown = set(request.query_params.keys()) - known
    if unknown:
        return 0, 0, JSONResponse(
            {"error": f"unsupported query parameters: {sorted(unknown)}"}, status_code=400
        )
    try:
        limit = int(request.query_params.get("limit", "100"))
        offset = int(request.query_params.get("offset", "0"))
    except ValueError:
        return 0, 0, JSONResponse({"error": "limit/offset must be integers"}, status_code=400)
    if limit < 1 or limit > 1000 or offset < 0:
        return 0, 0, JSONResponse({"error": "invalid limit/offset"}, status_code=400)
    return limit, offset, None


def _multipart_dicom(instances) -> StreamingResponse:
    boundary = "dicomweb-boundary-7f3c2a"
    chunks = []
    for item in instances:
        chunks.append(
            f"--{boundary}\r\nContent-Type: {DICOM}\r\n\r\n".encode("ascii")
        )
        chunks.append(item.path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    media_type = f"multipart/related; type=\"{DICOM}\"; boundary={boundary}"
    return StreamingResponse(iter(chunks), media_type=media_type)


app = create_app()
