from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=Path(__file__).resolve().parents[2] / ".env", env_file_encoding="utf-8", extra="ignore")

    APP_NAME: str = "A.GCareerSathi"
    APP_ENV: str = "development"

    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7

    DATABASE_URL: str = ""
    MONGODB_URL: str = ""
    MONGODB_DB_NAME: str = "agcareersathi"
    MONGODB_DB_ALIASES: str = ""
    FRONTEND_ORIGIN: str = "https://agcareersathi.vercel.app"
    FRONTEND_ORIGINS: str = ""

    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-1.5-flash"

    RAZORPAY_KEY_ID: str = ""
    RAZORPAY_KEY_SECRET: str = ""

    EMAIL_HOST: str = ""
    EMAIL_PORT: int = 587
    EMAIL_USE_TLS: bool = True
    EMAIL_HOST_USER: str = ""
    EMAIL_HOST_PASSWORD: str = ""
    EMAIL_FROM_NAME: str = "A.GCareerSathi"

    SERPAPI_API_KEY: str = ""
    ADMIN_API_KEY: str = ""
    ADMIN_EMAIL: str = ""
    ADMIN_PASSWORD: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
