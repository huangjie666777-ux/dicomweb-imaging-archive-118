"""QIDO-RS: search for studies, series and instances."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydicom.datadict import tag_for_keyword

from .dicomutil import INSTANCE_KEYS, SERIES_KEYS, STUDY_KEYS
from .store import Store

router = APIRouter()

_KEY_TAGS = {kw: f"{tag_for_keyword(kw):08X}" for kw in
             set(STUDY_KEYS + SERIES_KEYS + INSTANCE_KEYS)}


def _project(metadata: dict, keys: list[str], extra: dict) -> dict:
    out = {}
    for kw in keys:
        tag = _KEY_TAGS.get(kw)
        if tag and tag in metadata:
            out[tag] = metadata[tag]
    out.update(extra)
    return out


def _uid_extra(row, level: str) -> dict:
    extra = {}
    if level in ("series", "instance"):
        extra["0020000D"] = {"vr": "UI", "Value": [row["study_instance_uid"]]}
    if level == "instance":
        extra["0020000E"] = {"vr": "UI", "Value": [row["series_instance_uid"]]}
    return extra


def _search(request: Request, level: str, path_uids: dict):
    store: Store = request.app.state.store
    params = request.query_params
    filters = dict(path_uids)
    for key in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
                "PatientID", "StudyDate"):
        value = params.get(key)
        if value:
            filters[key] = value
    try:
        limit = params.get("limit")
        limit = int(limit) if limit is not None else None
        offset = int(params.get("offset", 0))
        if (limit is not None and limit < 0) or offset < 0:
            raise ValueError
    except ValueError:
        return JSONResponse({"error": "invalid limit/offset"}, status_code=400)
    keys = {"study": STUDY_KEYS, "series": SERIES_KEYS, "instance": INSTANCE_KEYS}[level]
    rows = store.query(level, filters, limit, offset)
    return [
        _project(json.loads(row["metadata"]), keys, _uid_extra(row, level))
        for row in rows
    ]


@router.get("/studies")
async def search_studies(request: Request):
    return _search(request, "study", {})


@router.get("/series")
async def search_all_series(request: Request):
    return _search(request, "series", {})


@router.get("/studies/{study_uid}/series")
async def search_series(request: Request, study_uid: str):
    return _search(request, "series", {"StudyInstanceUID": study_uid})


@router.get("/instances")
async def search_all_instances(request: Request):
    return _search(request, "instance", {})


@router.get("/studies/{study_uid}/series/{series_uid}/instances")
async def search_instances(request: Request, study_uid: str, series_uid: str):
    return _search(request, "instance",
                   {"StudyInstanceUID": study_uid, "SeriesInstanceUID": series_uid})
