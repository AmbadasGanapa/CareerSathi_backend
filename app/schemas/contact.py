from pydantic import BaseModel, EmailStr, Field


class ContactCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    subject: str = Field(min_length=2, max_length=200)
    message: str = Field(min_length=2, max_length=5000)


class ContactCreateResponse(BaseModel):
    id: int
    detail: str
