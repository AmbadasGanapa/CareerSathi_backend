import json
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from app.api.deps.auth import get_db, get_current_user
from app.models.recommendation import Recommendation
from app.models.user import User
from app.models.payment import Payment
from app.schemas.recommendation import (
    RecommendationInput,
    RecommendationResponse,
    RecommendationGenerateResponse,
)
from app.services.gemini import generate_recommendation
from app.services.email import send_email


router = APIRouter(prefix="/recommendations", tags=["recommendations"])

OTHER_PREFIX = "other"

QUESTIONS = {
    "q1": "Which subjects do you enjoy the most?",
    "q2": "Are you willing to invest several years in education for your career?",
    "q3": "What type of activities do you enjoy?",
    "q4": "What kind of problems do you like solving?",
    "q5": "What kind of tasks do you enjoy most?",
    "q6": "What are you naturally good at?",
    "q7": "What are your strongest skills?",
    "q8": "How do you prefer to work?",
    "q9": "How do you usually make decisions?",
    "q10": "How do you handle pressure?",
    "q11": "What role do you see yourself in?",
    "q12": "Which environment do you prefer?",
    "q13": "Which career area attracts you most?",
    "q14": "Where do you see yourself working?",
    "q15": "What motivates you the most?",
    "q16": "How important is work-life balance to you?",
    "q17": "What kind of impact do you want to make?",
    "q18": "What is your main career goal?",
    "q19": "How comfortable are you with technology?",
    "q20": "What type of learning do you prefer?"
}


def format_answer(answer) -> str:
    selections = []
    for item in answer.selections:
        if not item:
            continue
        if item.strip().lower().startswith(OTHER_PREFIX):
            continue
        selections.append(item)
    if answer.other and answer.other.strip():
        selections.append(answer.other.strip())
    if not selections:
        return "No response"
    return "; ".join(selections)


def render_assessment(answers: dict) -> str:
    lines = []
    for qid, text in QUESTIONS.items():
        answer = answers.get(qid)
        if answer is None:
            rendered = "No response"
        else:
            rendered = format_answer(answer)
        lines.append(f"{qid.upper()}: {text}\nAnswer: {rendered}")
    return "\n".join(lines)


def _parse_json_response(raw: str) -> dict:
    if not raw:
        raise ValueError("Empty response")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            return json.loads(raw[start : end + 1])
        raise


def _is_paid(db: Session, user_id: int) -> bool:
    return (
        db.query(Payment)
        .filter(Payment.user_id == user_id, Payment.status == "paid")
        .first()
        is not None
    )


@router.post("/generate", response_model=RecommendationGenerateResponse)
def generate(
    payload: RecommendationInput,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    assessment_block = render_assessment(payload.answers)

    prompt = f"""
You are a career counselor for high school and junior college students in India.
Return ONLY valid JSON with keys:
- summary (string)
- top_branches (array of exactly 3 objects with branch, why_fit, courses, actions, outlook, demand, salary_range, careers)
- next_steps (array of strings)
- scholarships (array of strings)

Rules:
- Recommend branches/streams only (e.g., Science, Commerce, Arts, Diploma/Polytechnic, ITI, Design & Media, Medical & Allied, Engineering).
- Do NOT recommend full degree names like B.Tech or MBBS.
- Each top_branches item must include:
  - branch: string (stream/branch name)
  - why_fit: 1-2 sentences
  - courses: array of 4-6 course/specialization examples inside that branch
  - actions: array of 3-5 steps to achieve this branch (exams, skills, portfolio, entrance prep)
  - outlook: short sentence on future outlook
  - demand: short sentence on demand in India
  - salary_range: short range (e.g., "?3-8 LPA" or "?2.5-6 LPA")
  - careers: array of 4-6 roles students can become
- Rank branches fairly based on the student's skills/interests (put the best-fit first).

Student profile:
Name: {payload.name}
Education level: {payload.education_level}
Previous stream/branch: {payload.prior_stream or 'Not specified'}
Preferred subjects: {', '.join(payload.preferred_subjects)}
Strengths: {', '.join(payload.strengths)}
Interests: {', '.join(payload.interests)}
Career goals: {payload.career_goals or 'Not specified'}
Location: {payload.location or 'Not specified'}
Extra notes: {payload.extra_notes or 'Not specified'}

Assessment responses:
{assessment_block}
"""

    try:
        raw = generate_recommendation(prompt)
        data = _parse_json_response(raw)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Recommendation engine failed: {exc}")

    record = Recommendation(user_id=current_user.id, input_data=payload.model_dump(), output_data=data)
    db.add(record)
    db.commit()

    # Email: report ready to unlock
    ready_body = (
        f"Hi {current_user.name},\n\n"
        "Your CareerSpark report is ready!\n"
        "Complete the Rs 9 payment to unlock your full recommendations.\n\n"
        "Log in and open your results page to proceed."
    )
    background_tasks.add_task(
        send_email,
        current_user.email,
        "Your CareerSpark report is ready",
        ready_body,
    )

    return RecommendationGenerateResponse(report_ready=True)


@router.get("/latest", response_model=RecommendationResponse)
def latest(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not _is_paid(db, current_user.id):
        raise HTTPException(status_code=402, detail="Payment required")

    record = (
        db.query(Recommendation)
        .filter(Recommendation.user_id == current_user.id)
        .order_by(Recommendation.created_at.desc())
        .first()
    )
    if not record:
        raise HTTPException(status_code=404, detail="No recommendation found")

    return RecommendationResponse(recommendation=record.output_data)
