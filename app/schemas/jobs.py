from pydantic import BaseModel, HttpUrl


class JobItem(BaseModel):
    title: str
    company: str | None = None
    location: str | None = None
    source: str
    url: HttpUrl
    summary: str | None = None
    posted_at: str | None = None


class JobSearchResponse(BaseModel):
    jobs: list[JobItem]
    total: int
    providers: dict[str, str]
    message: str | None = None

