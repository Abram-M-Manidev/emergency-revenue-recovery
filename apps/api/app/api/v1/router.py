from fastapi import APIRouter

from app.api.v1.endpoints import (
    ai_conversations,
    auth,
    business_knowledge,
    health,
    vapi_webhooks,
    version,
    voice,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(version.router)
api_router.include_router(auth.router)
api_router.include_router(business_knowledge.router)
api_router.include_router(ai_conversations.router)
api_router.include_router(voice.router)
api_router.include_router(vapi_webhooks.router)
