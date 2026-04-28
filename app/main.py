from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import get_settings
from app.db.mongo import ensure_indexes, get_database


settings = get_settings()

app = FastAPI(title=settings.APP_NAME)

cors_origins = [settings.FRONTEND_ORIGIN.strip()] if settings.FRONTEND_ORIGIN else []
if settings.FRONTEND_ORIGINS:
    cors_origins.extend([item.strip() for item in settings.FRONTEND_ORIGINS.split(",") if item.strip()])
if not cors_origins:
    cors_origins = ["http://localhost:5173", "http://127.0.0.1:5173"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(dict.fromkeys(cors_origins)),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)


@app.on_event("startup")
def on_startup():
    try:
        get_database().command("ping")
        ensure_indexes()
        print("Mongo startup check completed.")
    except Exception as exc:
        # Fail-open so local API can still start even when Atlas is slow/unreachable.
        print(f"Mongo startup warning: {exc}")


# Lightweight ping endpoint (for uptime monitoring)
@app.api_route("/ping", methods=["GET", "HEAD"])
def ping():
    return {"status": "alive"}


# (You can keep this for health/debug)
@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(api_router, prefix="/api")
