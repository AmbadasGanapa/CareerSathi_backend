import datetime as dt
from fastapi import APIRouter, Depends, status
from pymongo.database import Database

from app.api.deps.auth import get_current_user_optional, get_db
from app.db.mongo import get_next_id
from app.schemas.contact import ContactCreateRequest, ContactCreateResponse


router = APIRouter(prefix="/contact", tags=["contact"])


@router.post("", response_model=ContactCreateResponse, status_code=status.HTTP_201_CREATED)
def submit_contact(
    payload: ContactCreateRequest,
    db: Database = Depends(get_db),
    current_user: dict | None = Depends(get_current_user_optional),
):
    contact_id = get_next_id("contacts", "contacts")
    now = dt.datetime.now(dt.timezone.utc)
    db["contacts"].insert_one(
        {
            "id": contact_id,
            "name": payload.name.strip(),
            "email": payload.email.strip().lower(),
            "subject": payload.subject.strip(),
            "message": payload.message.strip(),
            "user_id": current_user.get("id") if current_user else None,
            "created_at": now,
        }
    )
    return ContactCreateResponse(id=contact_id, detail="Contact details saved successfully.")
