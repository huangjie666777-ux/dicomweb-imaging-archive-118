"""STOW-RS: POST /studies storing multipart/related DICOM instances."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import config
from .dicomutil import InstanceRejection, metadata_json, parse_instance
from .multipart import MultipartError, parse_boundary, parse_parts
from .store import Conflict, Store

router = APIRouter()


def _ref(sop_class: str, sop_uid: str, reason: str | None = None) -> dict:
    ref = {
        "00081150": {"vr": "UI", "Value": [sop_class]},
        "00081155": {"vr": "UI", "Value": [sop_uid]},
    }
    if reason is not None:
        ref["00081197"] = {"vr": "US", "Value": [reason]}
    return ref


@router.post("/studies")
async def store_instances(request: Request):
    store: Store = request.app.state.store
    body = await request.body()
    if len(body) > config.MAX_UPLOAD_BYTES:
        return JSONResponse({"error": "upload exceeds size limit"}, status_code=413)
    try:
        boundary = parse_boundary(request.headers.get("content-type", ""))
        parts = list(parse_parts(body, boundary))
    except MultipartError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not parts:
        return JSONResponse({"error": "no instances in request"}, status_code=400)
    if len(parts) > config.MAX_INSTANCES_PER_REQUEST:
        return JSONResponse({"error": "too many instances in one request"}, status_code=413)

    successes, failures = [], []
    for headers, payload in parts:
        ctype = headers.get("content-type", "application/dicom").split(";")[0].strip()
        if ctype != "application/dicom":
            failures.append(_ref("", "", "unsupported part Content-Type"))
            continue
        try:
            ds = parse_instance(payload)
            sop_uid = str(ds.SOPInstanceUID)
            bulk_uri = (
                f"studies/{ds.StudyInstanceUID}/series/{ds.SeriesInstanceUID}"
                f"/instances/{sop_uid}"
            )
            meta = metadata_json(ds, bulk_uri)
            store.commit(ds, payload, meta)
            successes.append(_ref(str(ds.SOPClassUID), sop_uid))
        except InstanceRejection as exc:
            failures.append(_ref("unknown", "unknown", str(exc)))
        except Conflict as exc:
            failures.append(_ref(str(ds.SOPClassUID), str(ds.SOPInstanceUID), str(exc)))

    response = {"00081199": {"vr": "SQ", "Value": successes}}
    if failures:
        response["00081198"] = {"vr": "SQ", "Value": failures}
    return JSONResponse(response, status_code=200 if not failures else 409)
