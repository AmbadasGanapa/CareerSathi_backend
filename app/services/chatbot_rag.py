from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.services.gemini import generate_text_response

try:
    from langchain_core.documents import Document
    from langchain_core.prompts import ChatPromptTemplate
except Exception:  # pragma: no cover
    Document = None
    ChatPromptTemplate = None

try:
    from langchain_community.vectorstores import FAISS
except Exception:  # pragma: no cover
    FAISS = None

try:
    from langchain_google_genai import GoogleGenerativeAIEmbeddings
except Exception:  # pragma: no cover
    GoogleGenerativeAIEmbeddings = None

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except Exception:  # pragma: no cover
    RecursiveCharacterTextSplitter = None


settings = get_settings()
KNOWLEDGEBASE_DIR = Path(__file__).resolve().parents[1] / "knowledgebase"

_vector_store = None
_vector_signature = ""
_vector_lock = threading.Lock()


def _normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", " ", (text or "").lower()).strip()


def _token_set(text: str) -> set[str]:
    return {t for t in _normalize_text(text).split() if len(t) > 1}


def _compact_answer(text: str, max_sentences: int = 2, max_chars: int = 260) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return "I could not generate a response right now. Please try again."

    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    compact = " ".join([part.strip() for part in parts if part.strip()][:max_sentences]).strip()
    if not compact:
        compact = cleaned

    if len(compact) > max_chars:
        compact = compact[: max_chars - 3].rstrip(" ,;:") + "..."
    return compact


def _is_mutation_intent(question: str) -> bool:
    text = _normalize_text(question)
    destructive_terms = [
        "delete", "remove", "drop", "truncate", "update", "edit", "change",
        "insert", "create record", "modify", "reset", "clear data",
    ]
    return any(term in text for term in destructive_terms)


def _is_account_query(question: str) -> bool:
    text = _normalize_text(question)
    if "my report" in text or "my status" in text or "my history" in text or "my account" in text:
        return True
    triggers = [
        "my ", "attempt", "payment", "order id", "payment id", "recommended", "dashboard",
        "course for me", "branch for me", "account activity", "a c activity",
    ]
    return any(t in text for t in triggers)


def _quick_account_answer(question: str, user_context: dict[str, Any] | None) -> str | None:
    text = _normalize_text(question)

    if not user_context:
        if "payment" in text or "order id" in text or "payment id" in text or "account" in text or "history" in text:
            return "Please sign in to view your account activity."
        return None

    if "latest payment id" in text or ("payment id" in text and "latest" in text):
        pid = user_context.get("latest_payment_id")
        return f"Your latest payment ID is {pid}." if pid else "I could not find your latest payment ID yet."

    if "latest order id" in text or ("order id" in text and "latest" in text):
        oid = user_context.get("latest_order_id")
        return f"Your latest order ID is {oid}." if oid else "I could not find your latest order ID yet."

    if "how many" in text and ("attempt" in text or "test" in text):
        count = user_context.get("attempt_count", 0)
        return f"You have attempted the assessment {count} time{'s' if count != 1 else ''}."

    if "most recommended course" in text or ("recommended course" in text and "me" in text):
        course = user_context.get("most_recommended_course")
        return (
            f"Your most recommended course is {course}." if course else "I could not determine your most recommended course yet."
        )

    if "most recommended branch" in text or ("recommended branch" in text and "me" in text):
        branch = user_context.get("most_recommended_branch")
        return (
            f"Your most recommended branch is {branch}." if branch else "I could not determine your most recommended branch yet."
        )

    if "account activity" in text or "my account" in text or "my history" in text:
        latest_status = user_context.get("status_counts", {})
        latest_payment_id = user_context.get("latest_payment_id")
        attempt_count = user_context.get("attempt_count", 0)
        pieces = [f"You have attempted the assessment {attempt_count} time{'s' if attempt_count != 1 else ''}."]
        if latest_payment_id:
            pieces.append(f"Your latest payment ID is {latest_payment_id}.")
        if latest_status:
            pieces.append(f"Assessment status summary: {latest_status}.")
        return _compact_answer(" ".join(pieces))

    return None


def _quick_static_answer(question: str, docs: list["Document"]) -> str | None:
    text = _normalize_text(question)
    if "privacy" in text or "privacy policy" in text or "data safe" in text or "data privacy" in text:
        return _compact_answer(
            "Your data is used only for recommendation personalization and should be handled securely. "
            "For complete legal details, please refer to the Privacy Policy page."
        )

    if "refund" in text or "refund policy" in text:
        return _compact_answer(
            "Refunds are handled according to the published Refund Policy and payment verification status. "
            "Please check the Refund Policy page for full terms."
        )

    if "terms" in text or "terms and conditions" in text:
        return _compact_answer(
            "Please refer to the Terms and Conditions page for the complete rules, usage terms, and legal details."
        )

    if "how it works" in text or "how does it work" in text or "process" in text:
        return _compact_answer(
            "A.GCareerSathi works in 3 steps: answer the assessment, get AI-based career recommendations, "
            "and unlock your detailed report with guidance on branches, careers, and next steps."
        )

    if "account policy" in text or "policy notes" in text:
        return _compact_answer(
            "Account activity includes assessment attempts, recommendation trends, and payment status. "
            "Your data is used only for personalization and should be handled securely. "
            "For legal details, please refer to the Terms, Privacy Policy, and Refund Policy pages."
        )

    if "about" in text:
        return _compact_answer(
            "A.GCareerSathi helps Class 10, Class 12, and Diploma students explore suitable career paths. "
            "It also offers mentoring, resume review, career workshops, online identity guidance, study planning, and future skills support."
        )

    if not docs:
        return None

    # Prioritize very common FAQ intents with direct retrieval summary.
    quick_topics = ["faq", "price", "payment", "report", "about"]
    if not any(topic in text for topic in quick_topics):
        return None

    top = docs[:2]
    if not top:
        return None
    snippets = []
    for doc in top:
        content = doc.page_content.strip()
        if not content:
            continue
        short = content[:350].strip()
        snippets.append(short)
    if not snippets:
        return None
    return _compact_answer(" ".join(snippets))


def _as_dict(value: Any) -> dict:
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


def _load_knowledge_documents() -> list["Document"]:
    if Document is None:
        return []

    docs: list[Document] = []
    KNOWLEDGEBASE_DIR.mkdir(parents=True, exist_ok=True)
    txt_files = sorted(KNOWLEDGEBASE_DIR.glob("*.txt"))

    for path in txt_files:
        try:
            content = path.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        if not content:
            continue
        docs.append(
            Document(
                page_content=content,
                metadata={"source": f"kb/{path.name}", "title": path.stem.replace("_", " ").title(), "kind": "static"},
            )
        )

    return docs


def _knowledge_signature() -> str:
    parts: list[str] = []
    for p in sorted(KNOWLEDGEBASE_DIR.glob("*.txt")):
        try:
            parts.append(f"{p.name}:{int(p.stat().st_mtime)}:{p.stat().st_size}")
        except Exception:
            continue
    return "|".join(parts)


def _build_vector_store():
    if FAISS is None or GoogleGenerativeAIEmbeddings is None or RecursiveCharacterTextSplitter is None:
        return None
    if not settings.GEMINI_API_KEY:
        return None

    docs = _load_knowledge_documents()
    if not docs:
        return None

    splitter = RecursiveCharacterTextSplitter(chunk_size=900, chunk_overlap=140)
    split_docs = splitter.split_documents(docs)
    if not split_docs:
        return None

    try:
        embeddings = GoogleGenerativeAIEmbeddings(
            model="models/text-embedding-004",
            google_api_key=settings.GEMINI_API_KEY,
        )
        return FAISS.from_documents(split_docs, embeddings)
    except Exception:
        return None


def _get_vector_store():
    global _vector_store, _vector_signature
    with _vector_lock:
        sig = _knowledge_signature()
        if _vector_store is None or sig != _vector_signature:
            _vector_store = _build_vector_store()
            _vector_signature = sig
    return _vector_store


def _retrieve_docs(question: str, k: int = 4, use_vector: bool = True) -> list["Document"]:
    docs = _load_knowledge_documents()
    if not docs:
        return []

    store = _get_vector_store() if use_vector else None
    if use_vector and store is not None:
        try:
            found = store.similarity_search(question, k=k)
            if found:
                return found
        except Exception:
            pass

    # lexical fallback if vector search unavailable
    q_tokens = _token_set(question)
    scored: list[tuple[int, Document]] = []
    for doc in docs:
        score = len(q_tokens.intersection(_token_set(doc.page_content)))
        scored.append((score, doc))
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [doc for score, doc in scored[:k] if score > 0]
    return top or docs[: min(k, len(docs))]


def _format_context(docs: list["Document"]) -> str:
    chunks: list[str] = []
    for doc in docs:
        title = doc.metadata.get("title", "Context")
        source = doc.metadata.get("source", "unknown")
        chunks.append(f"[{title} | {source}]\n{doc.page_content}")
    return "\n\n".join(chunks)


def _build_user_activity_summary(db, user: dict[str, Any] | None) -> dict[str, Any] | None:
    if not user:
        return None

    user_id = user.get("id")
    if not isinstance(user_id, int):
        return None

    assessments = list(db["assessments"].find({"user_id": user_id}).sort("created_at", -1).limit(80))
    recommendations = list(db["recommendations"].find({"user_id": user_id}).sort("created_at", -1).limit(30))
    latest_payment = db["payments"].find_one({"user_id": user_id}, sort=[("created_at", -1)])

    status_counts: dict[str, int] = {}
    for item in assessments:
        status = str(item.get("status", "unknown"))
        status_counts[status] = status_counts.get(status, 0) + 1

    branch_counts: dict[str, int] = {}
    course_counts: dict[str, int] = {}
    for rec in recommendations:
        data = _as_dict(rec.get("output_data"))
        for branch in data.get("top_branches", [])[:3]:
            if not isinstance(branch, dict):
                continue
            bname = str(branch.get("branch", "")).strip()
            if bname:
                branch_counts[bname] = branch_counts.get(bname, 0) + 1
            for course in branch.get("courses", [])[:12]:
                cname = str(course).strip()
                if cname:
                    course_counts[cname] = course_counts.get(cname, 0) + 1

    most_recommended_branch = max(branch_counts, key=branch_counts.get) if branch_counts else None
    most_recommended_course = max(course_counts, key=course_counts.get) if course_counts else None

    latest_payment_status = latest_payment.get("status") if latest_payment else "none"
    latest_payment_amount = latest_payment.get("amount") if latest_payment else None
    latest_payment_id = latest_payment.get("payment_id") if latest_payment else None
    latest_order_id = latest_payment.get("order_id") if latest_payment else None

    recent_assessments = []
    for item in assessments[:10]:
        recent_assessments.append(
            {
                "assessment_id": item.get("id"),
                "status": item.get("status"),
                "recommendation_id": item.get("recommendation_id"),
                "created_at": str(item.get("created_at")),
            }
        )

    recent_payments = []
    payments = list(db["payments"].find({"user_id": user_id}).sort("created_at", -1).limit(8))
    for pay in payments:
        recent_payments.append(
            {
                "order_id": pay.get("order_id"),
                "payment_id": pay.get("payment_id"),
                "status": pay.get("status"),
                "amount_inr": pay.get("amount"),
                "created_at": str(pay.get("created_at")),
            }
        )

    recommendation_snapshots = []
    for rec in recommendations[:8]:
        data = _as_dict(rec.get("output_data"))
        top = []
        for branch in data.get("top_branches", [])[:3]:
            if isinstance(branch, dict) and branch.get("branch"):
                top.append(str(branch.get("branch")))
        recommendation_snapshots.append(
            {
                "recommendation_id": rec.get("id"),
                "top_branches": top,
                "created_at": str(rec.get("created_at")),
            }
        )

    summary = (
        f"User: {user.get('name', 'Student')} ({user.get('email', 'unknown')}). "
        f"Total assessment attempts: {len(assessments)}. "
        f"Status distribution: {status_counts}. "
        f"Most recommended branch: {most_recommended_branch or 'not available'}. "
        f"Most recommended course/specialization: {most_recommended_course or 'not available'}. "
        f"Latest payment status: {latest_payment_status}. "
    )
    if latest_payment_amount is not None:
        summary += f"Latest payment amount (INR): {latest_payment_amount}. "
    if latest_payment_id:
        summary += f"Latest payment ID: {latest_payment_id}. "
    if latest_order_id:
        summary += f"Latest order ID: {latest_order_id}. "
    summary += f"Generated at {datetime.utcnow().isoformat()} UTC."

    return {
        "summary": summary,
        "attempt_count": len(assessments),
        "status_counts": status_counts,
        "most_recommended_branch": most_recommended_branch,
        "most_recommended_course": most_recommended_course,
        "latest_payment_id": latest_payment_id,
        "latest_order_id": latest_order_id,
        "recent_assessments": recent_assessments,
        "recent_payments": recent_payments,
        "recommendation_snapshots": recommendation_snapshots,
    }


def ask_chatbot(question: str, db, user: dict[str, Any] | None) -> dict[str, Any]:
    if not question or not question.strip():
        return {
            "answer": "Please type a valid question.",
            "sources": [],
            "used_account_context": user is not None,
        }

    if Document is None or ChatPromptTemplate is None:
        return {
            "answer": "Chatbot dependencies are missing. Install requirements first to enable full RAG mode.",
            "sources": [],
            "used_account_context": user is not None,
        }

    if _is_mutation_intent(question):
        return {
            "answer": (
                "I can help with read-only account insights and policy guidance, "
                "but I cannot alter or delete your data."
            ),
            "sources": ["safety_read_only_policy"],
            "used_account_context": user is not None,
        }

    is_account_query = _is_account_query(question)
    static_docs = _retrieve_docs(question, k=4, use_vector=bool(is_account_query))

    # Fast path for account queries (no LLM call).
    if is_account_query:
        user_context = _build_user_activity_summary(db, user)
        quick = _quick_account_answer(question, user_context)
        if quick:
            return {
                "answer": quick,
                "sources": ["account_activity", "account_tables"],
                "used_account_context": user_context is not None,
            }
    else:
        quick_static = _quick_static_answer(question, static_docs)
        if quick_static:
            return {
                "answer": quick_static,
                "sources": [str(d.metadata.get("source", "kb")) for d in static_docs[:2]],
                "used_account_context": False,
            }

    user_context = _build_user_activity_summary(db, user)

    # Fast path for common static FAQ/policy/process queries.
    quick_static = _quick_static_answer(question, static_docs)
    if quick_static and not is_account_query:
        return {
            "answer": quick_static,
            "sources": [str(d.metadata.get("source", "kb")) for d in static_docs[:2]],
            "used_account_context": user_context is not None,
        }

    docs = list(static_docs)
    if user_context:
        docs.append(
            Document(
                page_content=user_context.get("summary", ""),
                metadata={"source": "account_activity", "title": "User account activity summary", "kind": "account"},
            )
        )
        docs.append(
            Document(
                page_content=json.dumps(
                    {
                        "attempt_count": user_context.get("attempt_count"),
                        "status_counts": user_context.get("status_counts"),
                        "most_recommended_branch": user_context.get("most_recommended_branch"),
                        "most_recommended_course": user_context.get("most_recommended_course"),
                        "latest_payment_id": user_context.get("latest_payment_id"),
                        "latest_order_id": user_context.get("latest_order_id"),
                        "recent_assessments": user_context.get("recent_assessments"),
                        "recent_payments": user_context.get("recent_payments"),
                        "recommendation_snapshots": user_context.get("recommendation_snapshots"),
                    },
                    ensure_ascii=True,
                ),
                metadata={"source": "account_tables", "title": "User account tables snapshot", "kind": "account"},
            )
        )

    context_text = _format_context(docs)
    if not context_text.strip():
        context_text = "No knowledgebase context available."

    prompt = ChatPromptTemplate.from_template(
        """
You are AG.IO, the internal assistant for A.GCareerSathi.
Answer in a clear, helpful, and practical way.

Rules:
- Use provided context.
- For account questions, prioritize account_activity facts (attempt count, recommendations, payment/report status).
- You are read-only: never claim data was changed, deleted, or updated.
- If info is missing, say what is missing and suggest the next step.
- Keep the answer very short.
- Use at most 2 sentences.
- Do not add extra explanation unless the user asks for it.
- Answer only the exact question asked.

Question:
{question}

Context:
{context}
"""
    )

    prompt_value = prompt.invoke({"question": question.strip(), "context": context_text})
    prompt_text = prompt_value.to_string() if hasattr(prompt_value, "to_string") else str(prompt_value)
    if not prompt_text.strip():
        prompt_text = f"Question: {question.strip()}\n\nContext:\n{context_text}"

    try:
        answer = _compact_answer(generate_text_response(prompt_text, temperature=0.2, timeout_seconds=18).strip())
    except Exception as exc:
        fallback = _quick_account_answer(question, user_context) if is_account_query else _quick_static_answer(question, static_docs)
        answer = _compact_answer(fallback or "I could not generate a response right now. Please try again.")

    sources: list[str] = []
    for d in docs:
        src = str(d.metadata.get("source", "unknown"))
        if src not in sources:
            sources.append(src)

    return {
        "answer": _compact_answer(answer),
        "sources": sources[:6],
        "used_account_context": user_context is not None,
    }
