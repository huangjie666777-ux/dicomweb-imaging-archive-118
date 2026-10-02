"""FastAPI application wiring STOW/QIDO/WADO routers together."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from . import config
from .qido import router as qido_router
from .store import Store
from .stow import router as stow_router
from .wado import router as wado_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Store init also cleans up unpublished temp files from previous runs.
    app.state.store = Store()
    yield
    app.state.store.close()


app = FastAPI(title="DICOMweb backend", lifespan=lifespan)
app.include_router(stow_router)
app.include_router(qido_router)
app.include_router(wado_router)


@app.get("/health")
async def health():
    return {"status": "ok", "limits": {
        "max_upload_bytes": config.MAX_UPLOAD_BYTES,
        "max_instances_per_request": config.MAX_INSTANCES_PER_REQUEST,
    }}
