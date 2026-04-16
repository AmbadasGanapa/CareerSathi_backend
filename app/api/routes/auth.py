from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session

from app.api.deps.auth import get_db, get_current_user
from app.core.security import create_access_token
from app.models.user import User
from app.schemas.auth import SignupRequest, LoginRequest, TokenResponse, UserPublic
from app.utils.password import hash_password, verify_password
from app.services.email import send_email


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/signup", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    user = User(
        name=payload.name,
        email=payload.email,
        hashed_password=hash_password(payload.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    welcome_body = (
        f"Hi {user.name},\n\n"
        "Welcome to A.GCareerSathi!\n"
        "Your account is ready. Start your assessment to get personalized career recommendations.\n\n"
        "We are excited to have you with us."
    )
    background_tasks.add_task(
        send_email,
        user.email,
        "Welcome to A.GCareerSathi",
        welcome_body,
    )

    return user


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == payload.email).first()
    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(str(user.id))
    return TokenResponse(access_token=token)


@router.get("/me", response_model=UserPublic)
def me(current_user: User = Depends(get_current_user)):
    return current_user
