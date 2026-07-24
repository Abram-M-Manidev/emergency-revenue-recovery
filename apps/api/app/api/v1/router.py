from fastapi import APIRouter

from app.api.v1.endpoints import auth, business_knowledge, health, version

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(version.router)
api_router.include_router(auth.router)
api_router.include_router(business_knowledge.router)
