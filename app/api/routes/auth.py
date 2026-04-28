import re

from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from pymongo.database import Database
from pymongo.errors import ExecutionTimeout

from app.api.deps.auth import get_db, get_current_user
from app.core.config import get_settings
from app.core.security import create_access_token
from app.db.mongo import get_next_id, to_public_document
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, UserPublic
from app.utils.password import hash_password, verify_password
from app.services.email import send_email


router = APIRouter(prefix="/auth", tags=["auth"])
settings = get_settings()


def _candidate_user_dbs(primary_db: Database) -> list[Database]:
    raw_aliases = [item.strip() for item in settings.MONGODB_DB_ALIASES.split(",") if item.strip()]
    # Include common case-variants as fallback for migrated data.
    candidates = [primary_db.name, *raw_aliases, "careersathi", "CareerSathi"]
    seen: set[str] = set()
    ordered: list[Database] = []
    for name in candidates:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(primary_db.client[name])
    return ordered


def _find_user_by_email(db: Database, normalized_email: str, raw_email: str) -> tuple[dict | None, Database | None]:
    raw = raw_email.strip()
    # Fast path: exact matches only.
    exact_query = {
        "$or": [
            {"email": normalized_email},
            {"email_lookup": normalized_email},
            {"email_original": raw},
            {"email_original": normalized_email},
        ]
    }
    for candidate_db in _candidate_user_dbs(db):
        doc = candidate_db["users"].find_one(exact_query)
        if doc:
            return to_public_document(doc), candidate_db

    # Fallback path: case-insensitive match with strict server-side max time.
    escaped = re.escape(normalized_email)
    regex_filter = {"$regex": f"^{escaped}$", "$options": "i"}
    fallback_query = {
        "$or": [
            {"email": regex_filter},
            {"email_original": regex_filter},
        ]
    }
    for candidate_db in _candidate_user_dbs(db):
        try:
            doc = candidate_db["users"].find_one(fallback_query, max_time_ms=1500)
        except ExecutionTimeout:
            doc = None
        if doc:
            return to_public_document(doc), candidate_db
    return None, None


def _find_user_in_specific_db(db: Database, normalized_email: str, raw_email: str) -> dict | None:
    raw = raw_email.strip()
    exact_query = {
        "$or": [
            {"email": normalized_email},
            {"email_lookup": normalized_email},
            {"email_original": raw},
            {"email_original": normalized_email},
        ]
    }
    doc = db["users"].find_one(exact_query)
    if doc:
        return to_public_document(doc)

    escaped = re.escape(normalized_email)
    regex_filter = {"$regex": f"^{escaped}$", "$options": "i"}
    fallback_query = {
        "$or": [
            {"email": regex_filter},
            {"email_original": regex_filter},
        ]
    }
    try:
        return to_public_document(db["users"].find_one(fallback_query, max_time_ms=1500))
    except ExecutionTimeout:
        return None


def _best_password_source(user: dict, plain_password: str) -> tuple[bool, bool]:
    candidate_values = [
        user.get("hashed_password"),
        user.get("password_hash"),
        user.get("hashedPassword"),
        user.get("password"),
    ]
    used_plaintext_legacy = False
    for value in candidate_values:
        if not isinstance(value, str) or not value:
            continue
        if verify_password(plain_password, value):
            return True, used_plaintext_legacy
        if value == plain_password:
            used_plaintext_legacy = True
            return True, used_plaintext_legacy
    return False, used_plaintext_legacy


def _is_env_admin_email(email: str) -> bool:
    admin_email = (settings.ADMIN_EMAIL or "").strip().lower()
    return bool(admin_email and email.strip().lower() == admin_email)


def _login_env_admin(db: Database, normalized_email: str, raw_email: str, plain_password: str) -> dict:
    admin_password = (settings.ADMIN_PASSWORD or "").strip()
    if not admin_password:
        raise HTTPException(status_code=401, detail="Admin login is not configured")
    if plain_password != admin_password:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    existing, _ = _find_user_by_email(db, normalized_email, raw_email)
    if existing:
        updates = {
            "role": "admin",
            "email": normalized_email,
            "email_lookup": normalized_email,
            "email_original": raw_email.strip(),
            "hashed_password": hash_password(plain_password),
        }
        db["users"].update_one(
            {"id": existing["id"]},
            {"$set": updates},
        )
        existing.update(updates)
        return existing

    admin_user = {
        "id": get_next_id("users", "users"),
        "name": "Admin",
        "email": normalized_email,
        "email_lookup": normalized_email,
        "email_original": raw_email.strip(),
        "hashed_password": hash_password(plain_password),
        "role": "admin",
    }
    db["users"].insert_one(admin_user)
    return admin_user


@router.post("/signup", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, background_tasks: BackgroundTasks, db: Database = Depends(get_db)):
    normalized_email = payload.email.strip().lower()
    if _is_env_admin_email(normalized_email):
        raise HTTPException(status_code=400, detail="This email is reserved for admin login")
    existing, _ = _find_user_by_email(db, normalized_email, payload.email)
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = {
        "id": get_next_id("users", "users"),
        "name": payload.name,
        "email": normalized_email,
        "email_lookup": normalized_email,
        "email_original": payload.email.strip(),
        "hashed_password": hash_password(payload.password),
        "role": "user",
    }
    db["users"].insert_one(user)
    user_public = to_public_document(user)

    welcome_body = (
        f"Hi {user['name']},\n\n"
        "Welcome to A.GCareerSathi!\n"
        "Your account is ready. Start your assessment to get personalized career recommendations.\n\n"
        "We are excited to have you with us."
    )
    background_tasks.add_task(
        send_email,
        user["email"],
        "Welcome to A.GCareerSathi",
        welcome_body,
    )

    return user_public


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Database = Depends(get_db)):
    normalized_email = payload.email.strip().lower()

    if _is_env_admin_email(normalized_email):
        admin_user = _login_env_admin(db, normalized_email, payload.email, payload.password)
        token = create_access_token(str(admin_user["id"]))
        return TokenResponse(access_token=token, role="admin")

    user, source_db = _find_user_by_email(db, normalized_email, payload.email)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if source_db is not None and source_db.name.lower() != db.name.lower():
        existing_here = _find_user_in_specific_db(db, normalized_email, payload.email)
        if existing_here:
            user = existing_here
        else:
            candidate_id = user.get("id")
            if not isinstance(candidate_id, int) or db["users"].find_one({"id": candidate_id}):
                candidate_id = get_next_id("users", "users")
            migrated_user = {
                "id": candidate_id,
                "name": user.get("name") or "User",
                "email": normalized_email,
                "email_lookup": normalized_email,
                "email_original": payload.email.strip(),
                "role": user.get("role") or "user",
            }
            for key in ["hashed_password", "password_hash", "hashedPassword", "password"]:
                value = user.get(key)
                if isinstance(value, str) and value:
                    migrated_user[key] = value
            db["users"].insert_one(migrated_user)
            user = migrated_user

    user_id = user.get("id")
    if not isinstance(user_id, int):
        user_id = get_next_id("users", "users")
        db["users"].update_one(
            {"email": user.get("email")} if user.get("email") else {"email_original": payload.email.strip()},
            {"$set": {"id": user_id}},
        )

    verified, used_plaintext_legacy = _best_password_source(user, payload.password)

    if not verified:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Normalize migrated/legacy user records after successful login.
    updates: dict = {}
    if user.get("email") != normalized_email:
        updates["email"] = normalized_email
    if user.get("email_lookup") != normalized_email:
        updates["email_lookup"] = normalized_email
    if user.get("email_original") != payload.email.strip():
        updates["email_original"] = payload.email.strip()
    if not isinstance(user.get("role"), str) or not user.get("role"):
        updates["role"] = "user"
    stored_hash = user.get("hashed_password")
    if used_plaintext_legacy or not (isinstance(stored_hash, str) and stored_hash.startswith("pbkdf2_sha256$")):
        updates["hashed_password"] = hash_password(payload.password)

    if updates or used_plaintext_legacy or user.get("password_hash") or user.get("hashedPassword") or user.get("password"):
        db["users"].update_one(
            {"email": user.get("email")} if user.get("email") else {"id": user_id},
            {
                "$set": updates,
                "$unset": {
                    "password": "",
                    "password_hash": "",
                    "hashedPassword": "",
                },
            },
        )

    role = str(user.get("role") or updates.get("role") or "user").lower()
    token = create_access_token(str(user_id))
    return TokenResponse(access_token=token, role=role)


@router.get("/me", response_model=UserPublic)
def me(current_user=Depends(get_current_user)):
    if not isinstance(current_user.get("role"), str) or not current_user.get("role"):
        current_user["role"] = "user"
    return current_user
