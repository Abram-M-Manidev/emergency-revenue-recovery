from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.version import APP_VERSION

router = APIRouter(tags=["version"])


@router.get("/version", summary="API version and environment")
async def version(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {
        "version": APP_VERSION,
        "environment": settings.ENVIRONMENT,
    }
