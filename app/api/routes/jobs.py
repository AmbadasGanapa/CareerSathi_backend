from fastapi import APIRouter, Query

from app.schemas.jobs import JobSearchResponse
from app.services.jobs_search import search_jobs


router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/search", response_model=JobSearchResponse)
def jobs_search(
    q: str | None = Query(None, min_length=0, description="Job title or keyword"),
    location: str = Query("India", min_length=1),
    limit: int = Query(20, ge=5, le=50),
    source: str = Query("all", description="all | indeed | naukri | fallback"),
    work_mode: str = Query("any", description="any | remote | hybrid | onsite"),
    employment_type: str = Query("any", description="any | full_time | part_time | internship | contract | freelance"),
    recency_days: int = Query(30, ge=1, le=90),
):
    payload = search_jobs(
        query=q,
        location=location,
        limit=limit,
        source=source,
        work_mode=work_mode,
        employment_type=employment_type,
        recency_days=recency_days,
    )
    return JobSearchResponse(**payload)
