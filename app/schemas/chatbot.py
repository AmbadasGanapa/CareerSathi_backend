from pydantic import BaseModel, Field


class ChatbotAskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)


class ChatbotAskResponse(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list)
    used_account_context: bool = False
