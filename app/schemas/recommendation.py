from pydantic import BaseModel, Field


class AssessmentAnswer(BaseModel):
    selections: list[str] = Field(default_factory=list)
    other: str | None = None


class RecommendationInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: str | None = None
    education_level: str
    prior_stream: str | None = None
    answers: dict[str, AssessmentAnswer]
    interests: list[str]
    strengths: list[str]
    preferred_subjects: list[str]
    career_goals: str | None = None
    location: str | None = None
    extra_notes: str | None = None


class RecommendationResult(BaseModel):
    summary: str
    top_branches: list[dict]
    next_steps: list[str]
    scholarships: list[str] | None = None


class RecommendationResponse(BaseModel):
    recommendation: RecommendationResult


class RecommendationSubmitResponse(BaseModel):
    assessment_id: int


class RecommendationStatusResponse(BaseModel):
    status: str


class AssessmentHistoryItem(BaseModel):
    assessment_id: int
    status: str
    created_at: str
    education_level: str | None = None
    prior_stream: str | None = None
    top_branches: list[str] = Field(default_factory=list)


class RecommendationHistoryResponse(BaseModel):
    items: list[AssessmentHistoryItem]
