"""Liveness and readiness probes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.api.dependencies import DbDep
from app.core.config import settings

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health", summary="Liveness probe")
def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.environment}


@router.get("/health/ready", summary="Readiness probe (verifies PostgreSQL)")
def readiness(db: DbDep) -> dict[str, str]:
    db.execute(text("SELECT 1"))
    return {"status": "ready", "database": "connected"}
