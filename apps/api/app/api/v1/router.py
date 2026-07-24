from fastapi import APIRouter

from app.api.v1.endpoints import auth, health, version

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(version.router)
api_router.include_router(auth.router)
