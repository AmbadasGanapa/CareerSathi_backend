from fastapi import APIRouter

from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.contact import router as contact_router
from app.api.routes.jobs import router as jobs_router
from app.api.routes.recommendations import router as recommendation_router
from app.api.routes.payments import router as payments_router
from app.api.routes.chatbot import router as chatbot_router


api_router = APIRouter()
api_router.include_router(admin_router)
api_router.include_router(auth_router)
api_router.include_router(contact_router)
api_router.include_router(recommendation_router)
api_router.include_router(payments_router)
api_router.include_router(chatbot_router)
api_router.include_router(jobs_router)
