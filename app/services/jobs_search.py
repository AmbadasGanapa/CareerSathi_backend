from __future__ import annotations

import datetime as dt
import re
from urllib.parse import quote_plus, urlparse

import requests

from app.core.config import get_settings


settings = get_settings()
SERPAPI_URL = "https://serpapi.com/search.json"


def _parse_company_location(snippet: str) -> tuple[str | None, str | None]:
    if not snippet:
        return None, None
    parts = [p.strip() for p in snippet.split(" - ") if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return None, None


def _provider_query(provider: str, q: str, location: str) -> str:
    role_query = q.strip() if q and q.strip() else "hiring jobs"
    city_query = location.strip() if location and location.strip() else "India"
    if provider == "indeed":
        return f"site:indeed.com/jobs {role_query} {city_query}"
    return f"site:naukri.com {role_query} {city_query} jobs"


def _fetch_provider_jobs(provider: str, q: str, location: str, limit: int, recency_days: int) -> tuple[list[dict], str]:
    if not settings.SERPAPI_API_KEY:
        return [], "missing_api_key"

    params = {
        "api_key": settings.SERPAPI_API_KEY,
        "engine": "google",
        "q": _provider_query(provider, q, location),
        "num": min(max(limit, 5), 25),
        "hl": "en",
        "gl": "in",
    }
    if recency_days <= 1:
        params["tbs"] = "qdr:d"
    elif recency_days <= 7:
        params["tbs"] = "qdr:w"
    elif recency_days <= 31:
        params["tbs"] = "qdr:m"
    else:
        params["tbs"] = "qdr:y"
    try:
        response = requests.get(SERPAPI_URL, params=params, timeout=12)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        error_text = str(exc).lower()
        if "winerror 10013" in error_text or "forbidden by its access permissions" in error_text:
            return [], "blocked_network"
        return [], "failed"
    except Exception:
        return [], "failed"

    organic_results = payload.get("organic_results", []) or []
    jobs: list[dict] = []
    for row in organic_results:
        link = row.get("link")
        title = row.get("title")
        if not link or not title:
            continue

        snippet = (row.get("snippet") or "").strip()
        company, city = _parse_company_location(snippet)
        source_name = provider
        try:
            host = urlparse(link).netloc.lower()
            if "indeed" in host:
                source_name = "indeed"
            elif "naukri" in host:
                source_name = "naukri"
        except Exception:
            pass

        jobs.append(
            {
                "title": title.strip(),
                "company": company,
                "location": city or (location.strip() if location else None),
                "source": source_name,
                "url": link,
                "summary": snippet or None,
                "posted_at": row.get("date"),
            }
        )
        if len(jobs) >= limit:
            break

    return jobs, "ok"


def _posted_age_days(posted_at: str | None) -> int | None:
    if not posted_at:
        return None
    text = posted_at.strip().lower()
    if not text:
        return None

    if "today" in text or "just now" in text:
        return 0
    if "yesterday" in text:
        return 1

    hour_match = re.search(r"(\d+)\s*hour", text)
    if hour_match:
        return 0

    day_match = re.search(r"(\d+)\s*day", text)
    if day_match:
        return int(day_match.group(1))

    week_match = re.search(r"(\d+)\s*week", text)
    if week_match:
        return int(week_match.group(1)) * 7

    month_match = re.search(r"(\d+)\s*month", text)
    if month_match:
        return int(month_match.group(1)) * 30

    year_match = re.search(r"(\d+)\s*year", text)
    if year_match:
        return int(year_match.group(1)) * 365

    try:
        parsed = dt.datetime.strptime(posted_at.strip(), "%b %d, %Y").date()
        delta = (dt.date.today() - parsed).days
        return max(0, delta)
    except Exception:
        return None


def _normalize_filter_value(value: str | None, default: str) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned or default


def _matches_mode(job: dict, mode: str) -> bool:
    if mode == "any":
        return True
    haystack = " ".join(
        [
            str(job.get("title") or ""),
            str(job.get("summary") or ""),
            str(job.get("location") or ""),
        ]
    ).lower()
    if mode == "remote":
        return "remote" in haystack or "work from home" in haystack or "wfh" in haystack
    if mode == "hybrid":
        return "hybrid" in haystack
    if mode == "onsite":
        return not _matches_mode(job, "remote") and not _matches_mode(job, "hybrid")
    return True


def _matches_employment(job: dict, employment_type: str) -> bool:
    if employment_type == "any":
        return True
    haystack = " ".join([str(job.get("title") or ""), str(job.get("summary") or "")]).lower()
    rules = {
        "full_time": ["full time", "full-time", "permanent"],
        "part_time": ["part time", "part-time"],
        "internship": ["intern", "internship", "trainee"],
        "contract": ["contract", "contractual", "temporary"],
        "freelance": ["freelance", "freelancer"],
    }
    return any(token in haystack for token in rules.get(employment_type, []))


def _apply_filters(
    jobs: list[dict],
    source: str,
    work_mode: str,
    employment_type: str,
    recency_days: int,
    limit: int,
) -> list[dict]:
    filtered: list[dict] = []
    for job in jobs:
        job_source = str(job.get("source") or "").lower()
        if source != "all" and job_source != source:
            continue
        # Fallback rows are generic search-entry points, so strict mode/employment
        # keyword filtering hides everything unexpectedly.
        if job_source != "fallback":
            if not _matches_mode(job, work_mode):
                continue
            if not _matches_employment(job, employment_type):
                continue

        age_days = _posted_age_days(job.get("posted_at"))
        if age_days is not None and age_days > recency_days:
            continue
        filtered.append(job)

    def _sort_key(item: dict) -> tuple[int, int]:
        age = _posted_age_days(item.get("posted_at"))
        has_date_rank = 0 if age is not None else 1
        age_rank = age if age is not None else 9999
        return has_date_rank, age_rank

    filtered.sort(key=_sort_key)
    return filtered[:limit]


def _fallback_jobs(query: str, location: str, limit: int, source_hint: str = "fallback") -> list[dict]:
    city = location.strip() if location and location.strip() else "India"
    role_seed = query.strip() if query and query.strip() else ""
    popular_roles = [
        "Software Engineer",
        "Data Analyst",
        "UI UX Designer",
        "Digital Marketing Executive",
        "Sales Executive",
        "Business Analyst",
        "Customer Support Specialist",
        "Accountant",
        "HR Recruiter",
        "Project Coordinator",
        "Content Writer",
        "Graphic Designer",
        "Mechanical Engineer",
        "Electrical Engineer",
        "Civil Engineer",
        "Operations Executive",
        "Teaching Assistant",
        "Nurse",
        "Lab Technician",
        "Supply Chain Analyst",
    ]
    roles: list[str] = []
    if role_seed:
        roles.append(role_seed)
    for role in popular_roles:
        if role.lower() not in {r.lower() for r in roles}:
            roles.append(role)
        if len(roles) >= limit:
            break

    normalized_source = source_hint.strip().lower() if source_hint else "fallback"
    if normalized_source not in {"indeed", "naukri", "fallback"}:
        normalized_source = "fallback"

    jobs: list[dict] = []
    for role in roles[:limit]:
        if normalized_source == "naukri":
            url = f"https://www.naukri.com/{quote_plus(role)}-jobs-in-{quote_plus(city)}"
        else:
            url = f"https://www.indeed.com/jobs?q={quote_plus(role)}&l={quote_plus(city)}"
        jobs.append(
            {
                "title": role,
                "company": None,
                "location": city,
                "source": normalized_source,
                "url": url,
                "summary": None,
                "posted_at": None,
            }
        )
    return jobs


def search_jobs(
    query: str | None,
    location: str,
    limit: int = 20,
    source: str = "all",
    work_mode: str = "any",
    employment_type: str = "any",
    recency_days: int = 30,
) -> dict:
    q = (query or "").strip()
    city = (location or "").strip()
    capped_limit = min(max(limit, 5), 50)

    half = max(3, capped_limit // 2)
    indeed_jobs, indeed_status = _fetch_provider_jobs("indeed", q, city, half, recency_days)
    naukri_jobs, naukri_status = _fetch_provider_jobs("naukri", q, city, half, recency_days)

    merged: list[dict] = []
    seen_urls: set[str] = set()
    for item in indeed_jobs + naukri_jobs:
        url = item.get("url")
        if not isinstance(url, str) or url in seen_urls:
            continue
        seen_urls.add(url)
        merged.append(item)
        if len(merged) >= capped_limit:
            break

    message = None
    both_blocked = indeed_status == "blocked_network" and naukri_status == "blocked_network"
    both_missing_key = indeed_status == "missing_api_key" and naukri_status == "missing_api_key"
    both_failed = indeed_status == "failed" and naukri_status == "failed"

    filter_source = _normalize_filter_value(source, "all")
    fallback_source_hint = filter_source if filter_source in {"indeed", "naukri", "fallback"} else "fallback"

    if not merged and (both_blocked or both_missing_key or both_failed):
        merged = _fallback_jobs(q, city, capped_limit, source_hint=fallback_source_hint)

    if not settings.SERPAPI_API_KEY:
        message = "SERPAPI_API_KEY is missing. Showing fallback job types for now."
    elif both_blocked:
        message = "Live job providers are blocked by system/network settings. Showing fallback job types."
    elif not merged and both_failed:
        message = "Could not fetch jobs right now. Please try again."
    elif not q:
        message = "Showing all job types for your selected location."

    filter_work_mode = _normalize_filter_value(work_mode, "any")
    filter_employment = _normalize_filter_value(employment_type, "any")
    filtered_merged = _apply_filters(
        jobs=merged,
        source=filter_source,
        work_mode=filter_work_mode,
        employment_type=filter_employment,
        recency_days=recency_days,
        limit=capped_limit,
    )
    filters_active = filter_source != "all" or filter_work_mode != "any" or filter_employment != "any" or recency_days != 30
    if not filtered_merged and merged and filters_active:
        message = "No jobs matched your current filters. Try broader filters."

    return {
        "jobs": filtered_merged,
        "total": len(filtered_merged),
        "providers": {"indeed": indeed_status, "naukri": naukri_status},
        "message": message,
    }
