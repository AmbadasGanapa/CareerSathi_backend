import json
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps.auth import get_db, get_current_user
from app.db.session import SessionLocal
from app.models.assessment import Assessment
from app.models.payment import Payment
from app.models.recommendation import Recommendation
from app.models.user import User
from app.schemas.recommendation import (
    RecommendationInput,
    RecommendationResponse,
    RecommendationHistoryResponse,
    AssessmentHistoryItem,
    RecommendationSubmitResponse,
    RecommendationStatusResponse,
)
from app.services.gemini import generate_recommendation
from app.services.email import send_email
from app.services.report_pdf import build_report_pdf


router = APIRouter(prefix="/recommendations", tags=["recommendations"])

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
    is_10th = "10" in education or "x" in education or "class 10" in education
    is_commerce = "commerce" in prior_stream or "commerce" in text
    is_science = "science" in prior_stream or "science" in text
    is_arts = "arts" in prior_stream or "humanities" in prior_stream or "arts" in text or "humanities" in text

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
        if is_12th:
            defaults = ["Commerce & Management", "IT & Computer Apps", "Arts & Humanities"]
        else:
            defaults = ["Science", "Commerce", "Arts & Humanities"]
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
                "Create a study plan for the next 3 months"
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
            "Review scholarship and exam dates"
        ],
        "scholarships": [
            "State merit scholarships",
            "National scholarship portal",
            "Institute-specific entrance scholarships"
        ]
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
    db = SessionLocal()
    assessment = None
    try:
        assessment = db.get(Assessment, assessment_id)
        user = db.get(User, user_id)
        if not assessment or not user:
            return

        payload_data = RecommendationInput(**assessment.input_data)
        data = generate_recommendation_from_payload(payload_data)
        rec = Recommendation(user_id=user.id, input_data=assessment.input_data, output_data=data)
        db.add(rec)
        db.commit()
        db.refresh(rec)

        assessment.recommendation_id = rec.id
        assessment.status = "complete"
        db.commit()

        report_name = payload_data.name or user.name
        report_email = payload_data.email or user.email
        pdf_bytes = build_report_pdf(data, report_name, report_email, assessment_id)
        summary_body = (
            f"Hi {user.name},\n\n"
            "Your A.GCareerSathi report is ready. We've attached the PDF copy for you.\n\n"
            "Log in to view the interactive report in your dashboard."
        )
        send_email(
            user.email,
            "Your A.GCareerSathi report is ready",
            summary_body,
            attachments=[("careerspark-report.pdf", pdf_bytes, "application/pdf")],
        )
    except Exception as exc:
        if assessment:
            assessment.status = "failed"
            db.commit()
        print(f"Recommendation generation failed for assessment {assessment_id}: {exc}")
    finally:
        db.close()


@router.post("/submit", response_model=RecommendationSubmitResponse)
def submit(payload: RecommendationInput, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    record = Assessment(user_id=current_user.id, input_data=payload.model_dump(), status="pending_payment")
    db.add(record)
    db.commit()
    db.refresh(record)
    return RecommendationSubmitResponse(assessment_id=record.id)


@router.get("/status/{assessment_id}", response_model=RecommendationStatusResponse)
def status(assessment_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    record = db.get(Assessment, assessment_id)
    if not record or record.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return RecommendationStatusResponse(status=record.status)


@router.post("/retry/{assessment_id}")
def retry(assessment_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    record = db.get(Assessment, assessment_id)
    if not record or record.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Assessment not found")

    paid = (
        db.query(Payment)
        .filter(Payment.assessment_id == assessment_id, Payment.user_id == current_user.id, Payment.status == "paid")
        .order_by(Payment.paid_at.desc())
        .first()
    )
    if not paid:
        raise HTTPException(status_code=402, detail="Payment required")

    record.status = "processing"
    db.commit()

    background_tasks.add_task(_generate_report_for_assessment, record.id, current_user.id)
    return {"status": "processing"}


@router.get("/result/{assessment_id}", response_model=RecommendationResponse)
def result(assessment_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    record = db.get(Assessment, assessment_id)
    if not record or record.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if record.status != "complete" or not record.recommendation_id:
        raise HTTPException(status_code=402, detail="Payment required")

    rec = db.get(Recommendation, record.recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    return RecommendationResponse(recommendation=rec.output_data)


@router.get("/report/{assessment_id}")
def report_pdf(assessment_id: int, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    record = db.get(Assessment, assessment_id)
    if not record or record.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if record.status != "complete" or not record.recommendation_id:
        raise HTTPException(status_code=402, detail="Payment required")

    rec = db.get(Recommendation, record.recommendation_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Recommendation not found")

    report_name = rec.input_data.get("name") if isinstance(rec.input_data, dict) else current_user.name
    report_email = rec.input_data.get("email") if isinstance(rec.input_data, dict) else current_user.email
    pdf_bytes = build_report_pdf(rec.output_data, report_name or current_user.name, report_email or current_user.email, assessment_id)
    filename = f"careerspark-report-{assessment_id}.pdf"
    return StreamingResponse(
        iter([pdf_bytes]),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.get("/history", response_model=RecommendationHistoryResponse)
def history(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    records = (
        db.query(Assessment)
        .filter(Assessment.user_id == current_user.id)
        .order_by(Assessment.created_at.desc())
        .limit(20)
        .all()
    )

    items: list[AssessmentHistoryItem] = []
    for record in records:
        top_branches: list[str] = []
        if record.recommendation_id:
            rec = db.get(Recommendation, record.recommendation_id)
            if rec and isinstance(rec.output_data, dict):
                for branch in rec.output_data.get("top_branches", [])[:3]:
                    name = branch.get("branch")
                    if name:
                        top_branches.append(str(name))

        input_data = record.input_data or {}
        items.append(
            AssessmentHistoryItem(
                assessment_id=record.id,
                status=record.status,
                created_at=record.created_at.isoformat(),
                education_level=input_data.get("education_level"),
                prior_stream=input_data.get("prior_stream"),
                top_branches=top_branches,
            )
        )

    return RecommendationHistoryResponse(items=items)
