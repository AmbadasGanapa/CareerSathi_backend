from fastapi import APIRouter, Depends, HTTPException
from jose import JWTError
from pymongo.database import Database

from app.api.deps.auth import get_db, optional_oauth2_scheme
from app.core.security import decode_access_token
from app.schemas.chatbot import ChatbotAskRequest, ChatbotAskResponse
from app.services.chatbot_rag import ask_chatbot


router = APIRouter(prefix="/chatbot", tags=["chatbot"])


@router.post("/ask", response_model=ChatbotAskResponse)
def ask(payload: ChatbotAskRequest, db: Database = Depends(get_db), token: str | None = Depends(optional_oauth2_scheme)):
    current_user = None
    if token:
        try:
            payload_token = decode_access_token(token)
            user_id = int(payload_token.get("sub"))
            current_user = {
                "id": user_id,
                "email": payload_token.get("email"),
                "name": payload_token.get("name"),
            }
        except (JWTError, ValueError, TypeError):
            current_user = None

    try:
        result = ask_chatbot(payload.question, db, current_user)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return ChatbotAskResponse(**result)
