import json
import re
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pymongo import DESCENDING
from pymongo.database import Database

from app.api.deps.auth import get_current_user, get_db
from app.core.config import get_settings
from app.db.mongo import get_database, get_next_id, to_public_document
from app.schemas.recommendation import (
    AssessmentHistoryItem,
    RecommendationHistoryResponse,
    RecommendationInput,
    RecommendationResponse,
    RecommendationStatusResponse,
    RecommendationSubmitResponse,
)
from app.services.email import send_email
from app.services.gemini import generate_recommendation
from app.services.report_pdf import build_report_pdf


router = APIRouter(prefix="/recommendations", tags=["recommendations"])
settings = get_settings()

OTHER_PREFIX = "other"

QUESTIONS = {
    "q1": "Which subjects do you enjoy the most?",
    "q2": "Are you willing to invest several years in education for your career?",
    "q3": "What type of activities do you enjoy the most?",
    "q4": "What excites you the most?",
    "q5": "In your free time, what do you prefer?",
    "q6": "What is your strongest ability?",
    "q7": "Which skill do you enjoy using the most?",
    "q8": "How do you prefer to work?",
    "q9": "How do you usually make decisions?",
    "q10": "How do you react under pressure?",
    "q11": "What kind of role suits you best?",
    "q12": "Which work environment do you prefer?",
    "q13": "Which career field interests you the most?",
    "q14": "What type of career structure do you prefer?",
    "q15": "What motivates you the most?",
    "q16": "What matters most to you in your future?",
    "q17": "What kind of impact do you want to create?",
    "q18": "How comfortable are you with technology?",
    "q19": "How do you learn best?",
    "q20": "How quickly do you adapt to new tools or concepts?"
}


def _candidate_data_dbs(primary_db: Database) -> list[Database]:
    raw_aliases = [item.strip() for item in settings.MONGODB_DB_ALIASES.split(",") if item.strip()]
    candidates = [primary_db.name, *raw_aliases, "agcareersathi", "CareerSathi", "careersathi"]
    seen: set[str] = set()
    result: list[Database] = []
    for name in candidates:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(primary_db.client[name])
    return result


def _user_ids_for_db(candidate_db: Database, current_user: dict) -> set[int]:
    ids: set[int] = set()
    if isinstance(current_user.get("id"), int):
        ids.add(current_user["id"])

    email = (current_user.get("email") or "").strip().lower()
    if not email:
        return ids

    escaped = re.escape(email)
    regex_filter = {"$regex": f"^{escaped}$", "$options": "i"}
    matched = candidate_db["users"].find_one(
        {
            "$or": [
                {"email": email},
                {"email_lookup": email},
                {"email_original": regex_filter},
                {"email": regex_filter},
            ]
        }
    )
    if matched and isinstance(matched.get("id"), int):
        ids.add(matched["id"])
    return ids


def _find_assessment_across_dbs(primary_db: Database, assessment_id: int, current_user: dict) -> tuple[dict | None, Database | None]:
    for candidate_db in _candidate_data_dbs(primary_db):
        user_ids = list(_user_ids_for_db(candidate_db, current_user))
        if not user_ids:
            continue
        record = to_public_document(candidate_db["assessments"].find_one({"id": assessment_id, "user_id": {"$in": user_ids}}))
        if record:
            return record, candidate_db
    return None, None


def _find_recommendation_across_dbs(primary_db: Database, recommendation_id: int, preferred_db: Database | None = None) -> dict | None:
    if preferred_db is not None:
        rec = to_public_document(preferred_db["recommendations"].find_one({"id": recommendation_id}))
        if rec:
            return rec

    for candidate_db in _candidate_data_dbs(primary_db):
        rec = to_public_document(candidate_db["recommendations"].find_one({"id": recommendation_id}))
        if rec:
            return rec
    return None


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
            return json.loads(raw[start: end + 1])
        raise


def build_prompt(payload: RecommendationInput) -> str:
    assessment_block = render_assessment(payload.answers)
    education = (payload.education_level or "").lower()
    stream = (payload.prior_stream or "").lower()
    for hint in payload.preferred_subjects + payload.strengths + payload.interests:
        stream += f" {hint.lower()}"
    return f"""
You are a career counselor for high school and junior college students in India.
Return ONLY valid JSON with keys:
- summary (string)
- top_branches (array of exactly 3 objects with branch, match_score, why_fit, courses, actions, outlook, demand, salary_range, careers)
- next_steps (array of strings)
- scholarships (array of strings)

Rules:
- If the student is in/after 10th, recommend streams/branches only (e.g., Science, Commerce, Arts/Humanities, Diploma/Polytechnic, ITI, Design & Media, Sports).
- If the student is in/after 12th, recommend India-appropriate UG program tracks (e.g., Commerce & Management, IT & Computer Applications, Science (UG), Arts & Humanities, Design & Media, Law, Medical & Allied, Hospitality & Tourism).
- For 12th Commerce (without Maths/PCM), avoid Engineering and Medical recommendations.
- Do NOT recommend full degree names like MBBS as a top-level branch name; use branch names and list degree examples in courses.
- Each top_branches item must include:
  - branch: string (stream/branch name)
  - match_score: integer 60-99 indicating fit for this student
  - why_fit: 1-2 sentences
  - courses: array of 8-12 course/specialization examples inside that branch (India-specific)
  - actions: array of 3-5 steps to achieve this branch (exams, skills, portfolio, entrance prep)
  - outlook: short sentence on future outlook
  - demand: short sentence on demand in India
  - salary_range: short range (e.g., "Rs 3-8 LPA" or "Rs 2.5-6 LPA")
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
Eligibility hints (for you only):
- Education text: {education}
- Stream keywords: {stream}

Assessment responses:
{assessment_block}
"""


def _fallback_recommendation(payload: RecommendationInput) -> dict:
    text = " ".join(
        [
            " ".join(payload.preferred_subjects),
            " ".join(payload.strengths),
            " ".join(payload.interests),
            payload.career_goals or "",
            payload.prior_stream or "",
            payload.extra_notes or "",
        ]
    ).lower()
    education = (payload.education_level or "").lower()
    prior_stream = (payload.prior_stream or "").lower()
    has_math = any(k in text for k in ["math", "mathematics", "stats", "statistics"])
    has_pcm = any(k in text for k in ["pcm", "physics", "chemistry"]) and has_math
    has_bio = any(k in text for k in ["biology", "biotech", "botany", "zoology"])
    is_12th = "12" in education or "xii" in education or "class 12" in education
    is_commerce = "commerce" in prior_stream or "commerce" in text
    is_science = "science" in prior_stream or "science" in text

    library_10 = [
        {
            "branch": "Science",
            "keywords": ["science", "physics", "chemistry", "biology", "math", "research", "analytics"],
            "courses": ["PCM", "PCB", "PCMB", "Biotechnology", "Statistics", "Environmental Science", "Computer Science", "Home Science"],
            "careers": ["Research Scientist", "Data Analyst", "Lab Technologist", "Teacher", "Scientist"],
        },
        {
            "branch": "Commerce",
            "keywords": ["commerce", "business", "finance", "accounts", "economics", "marketing"],
            "courses": ["Accountancy", "Economics", "Business Studies", "Finance", "Marketing", "Entrepreneurship", "Banking", "Mathematics (Commerce)", "Informatics Practices"],
            "careers": ["Accountant", "Business Analyst", "Financial Analyst", "Marketing Executive", "Auditor"],
        },
        {
            "branch": "Arts & Humanities",
            "keywords": ["arts", "humanities", "history", "literature", "psychology", "sociology"],
            "courses": ["Psychology", "Political Science", "English Literature", "Sociology", "History", "Geography", "Fine Arts", "Home Science", "Mass Communication"],
            "careers": ["Psychologist", "Journalist", "Counselor", "Content Writer", "Teacher"],
        },
        {
            "branch": "Diploma & Polytechnic",
            "keywords": ["diploma", "polytechnic", "practical", "technical"],
            "courses": ["Mechanical", "Civil", "Electrical", "Automobile", "Computer Engineering", "Electronics", "Fashion Technology", "Interior Design"],
            "careers": ["Technician", "Draftsman", "Site Supervisor", "Junior Engineer", "QA Technician"],
        },
        {
            "branch": "Design & Media",
            "keywords": ["design", "creative", "ui", "ux", "media", "animation", "graphics"],
            "courses": ["Graphic Design", "UI/UX", "Animation", "Fashion Design", "Film & Media", "Photography", "Game Art", "Communication Design"],
            "careers": ["Graphic Designer", "UX Designer", "Animator", "Art Director", "Content Producer"],
        },
        {
            "branch": "Sports",
            "keywords": ["sports", "athlete", "fitness", "gym", "training"],
            "courses": ["Sports Science", "Sports Management", "Physical Education", "Coaching", "Sports Psychology", "Sports Nutrition", "Fitness Training", "Sports Analytics"],
            "careers": ["Coach", "Fitness Trainer", "Sports Manager", "Physio Assistant", "Performance Analyst"],
        },
    ]

    library_12 = [
        {
            "branch": "Commerce & Management",
            "keywords": ["commerce", "business", "finance", "accounts", "economics", "marketing", "entrepreneurship"],
            "courses": ["B.Com (General)", "B.Com (Hons)", "BBA", "BMS", "BFIA", "BA (Economics)", "B.Com (Accounting & Finance)", "B.Com (Banking & Insurance)", "CS Foundation", "CA Foundation"],
            "careers": ["Accountant", "Business Analyst", "Financial Analyst", "Marketing Executive", "Auditor"],
        },
        {
            "branch": "IT & Computer Apps",
            "keywords": ["computer", "coding", "programming", "software", "tech", "it"],
            "courses": ["BCA", "B.Sc Computer Science", "B.Sc IT", "B.Sc Data Science", "B.Sc AI", "B.Sc Cyber Security", "B.Sc Statistics", "B.Voc Software Dev", "BCA Cloud & DevOps", "B.Sc Computational Mathematics"],
            "careers": ["Software Developer", "Data Analyst", "QA Engineer", "Web Developer", "IT Support Engineer"],
        },
        {
            "branch": "Science (UG)",
            "keywords": ["science", "physics", "chemistry", "biology", "math", "research", "analytics"],
            "courses": ["B.Sc Physics", "B.Sc Chemistry", "B.Sc Mathematics", "B.Sc Statistics", "B.Sc Biotechnology", "B.Sc Microbiology", "B.Sc Environmental Science", "B.Sc Zoology", "B.Sc Botany", "B.Sc Computer Science"],
            "careers": ["Research Assistant", "Lab Technologist", "Data Analyst", "Quality Analyst", "Teacher"],
        },
        {
            "branch": "Arts & Humanities",
            "keywords": ["arts", "humanities", "history", "literature", "psychology", "sociology"],
            "courses": ["BA Psychology", "BA English", "BA Political Science", "BA Sociology", "BA History", "BA Economics", "BA Journalism & Mass Comm", "BA Public Administration", "BA Geography", "BA Philosophy"],
            "careers": ["Psychologist (Asst.)", "Journalist", "Counselor", "Content Writer", "Policy Researcher"],
        },
        {
            "branch": "Design & Media",
            "keywords": ["design", "creative", "ui", "ux", "media", "animation", "graphics"],
            "courses": ["B.Des", "BFA", "BA Animation", "B.Sc VFX", "BA Film & TV", "B.Sc Multimedia", "BA Graphic Design", "BA Communication Design", "BA Photography", "BA Game Design"],
            "careers": ["Graphic Designer", "UX Designer", "Animator", "Art Director", "Content Producer"],
        },
        {
            "branch": "Law",
            "keywords": ["law", "legal", "justice", "advocate"],
            "courses": ["BA LLB", "BBA LLB", "B.Com LLB", "LLB (3-year)"],
            "careers": ["Legal Associate", "Compliance Analyst", "Legal Advisor", "Judiciary Aspirant"],
        },
        {
            "branch": "Hospitality & Tourism",
            "keywords": ["hospitality", "hotel", "tourism", "travel", "culinary"],
            "courses": ["BHM", "B.Sc Hospitality & Hotel Administration", "BBA Hospitality", "BA Travel & Tourism", "Culinary Arts", "Food Production", "Front Office Management", "Housekeeping Management"],
            "careers": ["Hotel Manager", "Front Office Executive", "Chef", "Travel Consultant", "Event Coordinator"],
        },
        {
            "branch": "Medical & Allied",
            "keywords": ["medical", "doctor", "health", "nursing", "pharmacy", "biology"],
            "courses": ["B.Pharm", "B.Sc Nursing", "BPT", "B.Sc Allied Health", "B.Sc Nutrition", "B.Sc Medical Lab Tech", "B.Sc Radiology", "B.Sc Anesthesia Tech"],
            "careers": ["Pharmacist", "Nurse", "Physiotherapist", "Lab Technologist", "Radiology Technologist"],
        },
        {
            "branch": "Engineering & Technology",
            "keywords": ["engineering", "pcm", "physics", "chemistry", "math", "technology"],
            "courses": ["B.Tech CSE", "B.Tech ECE", "B.Tech Mechanical", "B.Tech Civil", "B.Tech EEE", "B.Tech AI & ML", "B.Tech Data Science", "B.Tech Mechatronics"],
            "careers": ["Software Engineer", "Electronics Engineer", "Mechanical Engineer", "Civil Engineer", "QA Engineer"],
        },
    ]

    library = library_12 if is_12th else library_10

    scored = []
    for item in library:
        score = sum(1 for k in item["keywords"] if k in text)
        scored.append((score, item))
    scored.sort(key=lambda x: x[0], reverse=True)

    top = [item for score, item in scored if score > 0][:3]
    if len(top) < 3:
        defaults = ["Commerce & Management", "IT & Computer Apps", "Arts & Humanities"] if is_12th else ["Science", "Commerce", "Arts & Humanities"]
        for name in defaults:
            if len(top) >= 3:
                break
            fallback = next((i for i in library if i["branch"] == name), None)
            if fallback and fallback not in top:
                top.append(fallback)

    if is_12th and is_commerce and not (has_math or has_pcm or is_science):
        top = [item for item in top if item["branch"] not in ["Engineering & Technology", "Medical & Allied"]]
        while len(top) < 3:
            for name in ["Commerce & Management", "IT & Computer Apps", "Arts & Humanities", "Design & Media", "Hospitality & Tourism"]:
                if len(top) >= 3:
                    break
                fallback = next((i for i in library_12 if i["branch"] == name), None)
                if fallback and fallback not in top:
                    top.append(fallback)

    if is_12th and is_science and not has_bio:
        top = [item for item in top if item["branch"] != "Medical & Allied"]

    def build_branch(item: dict, idx: int) -> dict:
        return {
            "branch": item["branch"],
            "match_score": 90 - (idx - 1) * 5,
            "why_fit": "Based on your responses, this stream aligns with your interests and strengths.",
            "courses": item["courses"],
            "actions": [
                "Review the syllabus and choose core subjects",
                "Build foundational skills with short courses",
                "Explore entrance exams and eligibility",
                "Create a study plan for the next 3 months",
            ],
            "outlook": "Positive growth with multiple specialization options.",
            "demand": "Steady demand in India across entry-level roles.",
            "salary_range": "Rs 3-8 LPA",
            "careers": item["careers"],
        }

    return {
        "summary": "Here is a streamlined recommendation based on your inputs.",
        "top_branches": [build_branch(item, idx) for idx, item in enumerate(top[:3], start=1)],
        "next_steps": [
            "Shortlist 2-3 colleges or programs",
            "Talk to a counselor or mentor",
            "Complete a skills mini-project",
            "Review scholarship and exam dates",
        ],
        "scholarships": [
            "State merit scholarships",
            "National scholarship portal",
            "Institute-specific entrance scholarships",
        ],
    }


def generate_recommendation_from_payload(payload: RecommendationInput) -> dict:
    prompt = build_prompt(payload)
    try:
        raw = generate_recommendation(prompt)
        return _parse_json_response(raw)
    except Exception as exc:
        print(f"Gemini generation failed, using fallback: {exc}")
        return _fallback_recommendation(payload)


def _generate_report_for_assessment(assessment_id: int, user_id: int) -> None:
    db = get_database()
    try:
        assessment = to_public_document(db["assessments"].find_one({"id": assessment_id}))
        user = to_public_document(db["users"].find_one({"id": user_id}))
        if not assessment or not user:
            return

        payload_data = RecommendationInput(**assessment["input_data"])
        data = generate_recommendation_from_payload(payload_data)

        recommendation = {
            "id": get_next_id("recommendations", "recommendations"),
            "user_id": user_id,
            "input_data": assessment["input_data"],
            "output_data": data,
            "created_at": datetime.utcnow(),
        }
        db["recommendations"].insert_one(recommendation)

        db["assessments"].update_one(
            {"id": assessment_id},
            {"$set": {"recommendation_id": recommendation["id"], "status": "complete"}},
        )

        report_name = payload_data.name or user["name"]
        report_email = payload_data.email or user["email"]
        pdf_bytes = build_report_pdf(data, report_name, report_email, assessment_id)
        summary_body = (
            f"Hi {user['name']},\n\n"
            "Your A.GCareerSathi report is ready. We've attached the PDF copy for you.\n\n"
            "Log in to view the interactive report in your dashboard."
        )
        send_email(
            user["email"],
            "Your A.GCareerSathi report is ready",
            summary_body,
            attachments=[("careerspark-report.pdf", pdf_bytes, "application/pdf")],
        )
    except Exception as exc:
        db["assessments"].update_one({"id": assessment_id}, {"$set": {"status": "failed"}})
        print(f"Recommendation generation failed for assessment {assessment_id}: {exc}")


@router.post("/submit", response_model=RecommendationSubmitResponse)
def submit(payload: RecommendationInput, db: Database = Depends(get_db), current_user=Depends(get_current_user)):
    assessment = {
        "id": get_next_id("assessments", "assessments"),
        "user_id": current_user["id"],
        "input_data": payload.model_dump(),
        "status": "pending_payment",
        "recommendation_id": None,
        "created_at": datetime.utcnow(),
    }
    db["assessments"].insert_one(assessment)
    return RecommendationSubmitResponse(assessment_id=assessment["id"])


@router.get("/status/{assessment_id}", response_model=RecommendationStatusResponse)
def status(assessment_id: int, db: Database = Depends(get_db), current_user=Depends(get_current_user)):
    record, _ = _find_assessment_across_dbs(db, assessment_id, current_user)
    if not record:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return RecommendationStatusResponse(status=record["status"])


@router.post("/retry/{assessment_id}")
def retry(assessment_id: int, background_tasks: BackgroundTasks, db: Database = Depends(get_db), current_user=Depends(get_current_user)):
    record = to_public_document(db["assessments"].find_one({"id": assessment_id, "user_id": current_user["id"]}))
    if not record:
        raise HTTPException(status_code=404, detail="Assessment not found")

    paid = to_public_document(
        db["payments"].find_one(
            {"assessment_id": assessment_id, "user_id": current_user["id"], "status": "paid"},
            sort=[("paid_at", DESCENDING)],
        )
    )
    if not paid:
        raise HTTPException(status_code=402, detail="Payment required")

    db["assessments"].update_one({"id": assessment_id}, {"$set": {"status": "processing"}})
    background_tasks.add_task(_generate_report_for_assessment, assessment_id, current_user["id"])
    return {"status": "processing"}


@router.get("/result/{assessment_id}", response_model=RecommendationResponse)
def result(assessment_id: int, db: Database = Depends(get_db), current_user=Depends(get_current_user)):
    record, source_db = _find_assessment_across_dbs(db, assessment_id, current_user)
    if not record:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if record.get("status") != "complete" or not record.get("recommendation_id"):
        raise HTTPException(status_code=402, detail="Payment required")

    rec = _find_recommendation_across_dbs(db, record["recommendation_id"], source_db)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    output_data = _as_dict(rec.get("output_data"))
    return RecommendationResponse(recommendation=output_data)


@router.get("/report/{assessment_id}")
def report_pdf(assessment_id: int, db: Database = Depends(get_db), current_user=Depends(get_current_user)):
    record, source_db = _find_assessment_across_dbs(db, assessment_id, current_user)
    if not record:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if record.get("status") != "complete" or not record.get("recommendation_id"):
        raise HTTPException(status_code=402, detail="Payment required")

    rec = _find_recommendation_across_dbs(db, record["recommendation_id"], source_db)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    input_data = _as_dict(rec.get("input_data"))
    report_name = input_data.get("name") or current_user["name"]
    report_email = input_data.get("email") or current_user["email"]
    pdf_bytes = build_report_pdf(_as_dict(rec.get("output_data")), report_name, report_email, assessment_id)
    filename = f"careerspark-report-{assessment_id}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _as_iso(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return datetime.utcnow().isoformat()


def _as_dict(value) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


@router.get("/history", response_model=RecommendationHistoryResponse)
def history(db: Database = Depends(get_db), current_user=Depends(get_current_user)):
    merged_records: list[tuple[dict, Database]] = []
    for candidate_db in _candidate_data_dbs(db):
        user_ids = list(_user_ids_for_db(candidate_db, current_user))
        if not user_ids:
            continue
        cursor = (
            candidate_db["assessments"]
            .find({"user_id": {"$in": user_ids}})
            .sort("created_at", DESCENDING)
            .limit(30)
        )
        for raw in cursor:
            record = to_public_document(raw)
            if record:
                merged_records.append((record, candidate_db))

    def _sort_key(item: tuple[dict, Database]) -> str:
        value = item[0].get("created_at")
        return _as_iso(value)

    merged_records.sort(key=_sort_key, reverse=True)

    items: list[AssessmentHistoryItem] = []
    seen_ids: set[int] = set()
    for record, source_db in merged_records:
        if len(items) >= 20:
            break
        assessment_id = record.get("id")
        if isinstance(assessment_id, int):
            if assessment_id in seen_ids:
                continue
            seen_ids.add(assessment_id)
        record = record or {}
        top_branches: list[str] = []
        rec_id = record.get("recommendation_id")
        if rec_id:
            rec = _find_recommendation_across_dbs(db, rec_id, source_db)
            output_data = _as_dict(rec.get("output_data")) if rec else {}
            if isinstance(output_data, dict):
                for branch in output_data.get("top_branches", [])[:3]:
                    name = branch.get("branch")
                    if name:
                        top_branches.append(str(name))

        input_data = _as_dict(record.get("input_data"))
        items.append(
            AssessmentHistoryItem(
                assessment_id=record.get("id", 0),
                status=record.get("status", "pending_payment"),
                created_at=_as_iso(record.get("created_at")),
                education_level=input_data.get("education_level"),
                prior_stream=input_data.get("prior_stream"),
                top_branches=top_branches,
            )
        )

    return RecommendationHistoryResponse(items=items)
