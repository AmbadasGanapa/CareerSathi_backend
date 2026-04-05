from fastapi import APIRouter

from app.api.routes.auth import router as auth_router
from app.api.routes.recommendations import router as recommendation_router
from app.api.routes.payments import router as payments_router


api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(recommendation_router)
api_router.include_router(payments_router)
