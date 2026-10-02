"""WADO-RS: retrieve instances and metadata as multipart/related or DICOM JSON."""
from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .multipart import render
from .store import Store

router = APIRouter()


def _multipart(payloads, content_type: str) -> Response:
    boundary = uuid.uuid4().hex
    body = render(payloads, boundary, content_type)
    return Response(
        body,
        media_type=f'multipart/related; type="{content_type}"; boundary={boundary}',
    )


def _not_found(detail: str):
    return JSONResponse({"error": detail}, status_code=404)


def _retrieve(request: Request, study_uid, series_uid, sop_uid, metadata: bool):
    store: Store = request.app.state.store
    rows = store.get_instances(study_uid, series_uid, sop_uid)
    if not rows:
        return _not_found("no instances match the requested hierarchy")
    if metadata:
        return [json.loads(row["metadata"]) for row in rows]
    return _multipart([store.read_bytes(row) for row in rows], "application/dicom")


@router.get("/studies/{study_uid}")
async def retrieve_study(request: Request, study_uid: str):
    return _retrieve(request, study_uid, None, None, metadata=False)


@router.get("/studies/{study_uid}/metadata")
async def study_metadata(request: Request, study_uid: str):
    return _retrieve(request, study_uid, None, None, metadata=True)


@router.get("/studies/{study_uid}/series/{series_uid}")
async def retrieve_series(request: Request, study_uid: str, series_uid: str):
    return _retrieve(request, study_uid, series_uid, None, metadata=False)


@router.get("/studies/{study_uid}/series/{series_uid}/metadata")
async def series_metadata(request: Request, study_uid: str, series_uid: str):
    return _retrieve(request, study_uid, series_uid, None, metadata=True)


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}")
async def retrieve_instance(request: Request, study_uid: str, series_uid: str, sop_uid: str):
    return _retrieve(request, study_uid, series_uid, sop_uid, metadata=False)


@router.get("/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata")
async def instance_metadata(request: Request, study_uid: str, series_uid: str, sop_uid: str):
    return _retrieve(request, study_uid, series_uid, sop_uid, metadata=True)
