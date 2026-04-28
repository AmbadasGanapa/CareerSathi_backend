from typing import Any, Generator
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError

from app.core.security import decode_access_token
from app.db.mongo import get_database, to_public_document


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")
optional_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

def get_db() -> Generator:
    yield get_database()


def get_current_user(token: str = Depends(oauth2_scheme), db=Depends(get_db)) -> dict[str, Any]:
    try:
        payload = decode_access_token(token)
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user = to_public_document(db["users"].find_one({"id": user_id}))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not isinstance(user.get("role"), str) or not user.get("role"):
        user["role"] = "user"
    return user


def get_current_user_optional(token: str | None = Depends(optional_oauth2_scheme), db=Depends(get_db)) -> dict[str, Any] | None:
    if not token:
        return None
    try:
        payload = decode_access_token(token)
        user_id = int(payload.get("sub"))
    except (JWTError, ValueError, TypeError):
        return None

    user = to_public_document(db["users"].find_one({"id": user_id}))
    if user and (not isinstance(user.get("role"), str) or not user.get("role")):
        user["role"] = "user"
    return user


def get_current_admin(current_user: dict[str, Any] = Depends(get_current_user)) -> dict[str, Any]:
    role = str(current_user.get("role") or "user").lower()
    if role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return current_user
