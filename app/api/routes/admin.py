from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Query
from pymongo import DESCENDING
from pymongo.database import Database

from app.api.deps.auth import get_current_admin, get_db


router = APIRouter(prefix="/admin", tags=["admin"])


def _date_floor_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _safe_iso(value) -> str | None:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return None


def _daily_series(
    db: Database,
    collection_name: str,
    date_field: str,
    start_date: dt.datetime,
    days: int,
    extra_match: dict | None = None,
) -> list[dict]:
    match_filter = {date_field: {"$gte": start_date}}
    if extra_match:
        match_filter.update(extra_match)

    pipeline = [
        {"$match": match_filter},
        {
            "$project": {
                "day": {
                    "$dateToString": {"format": "%Y-%m-%d", "date": f"${date_field}", "timezone": "UTC"}
                }
            }
        },
        {"$group": {"_id": "$day", "count": {"$sum": 1}}},
    ]
    grouped = list(db[collection_name].aggregate(pipeline))
    grouped_map = {item["_id"]: int(item["count"]) for item in grouped}

    output: list[dict] = []
    for offset in range(days):
        day = start_date + dt.timedelta(days=offset)
        label = day.strftime("%Y-%m-%d")
        output.append({"date": label, "count": grouped_map.get(label, 0)})
    return output


@router.get("/overview")
def admin_overview(
    days: int = Query(14, ge=7, le=90),
    _=Depends(get_current_admin),
    db: Database = Depends(get_db),
):
    now = dt.datetime.now(dt.timezone.utc)
    start_date = _date_floor_utc(now - dt.timedelta(days=days - 1))
    active_since = now - dt.timedelta(days=30)

    total_users = db["users"].count_documents({})
    total_contacts = db["contacts"].count_documents({})
    total_assessments = db["assessments"].count_documents({})
    total_recommendations = db["recommendations"].count_documents({})
    total_paid_payments = db["payments"].count_documents({"status": "paid"})
    total_sessions = db["sessions"].count_documents({})
    total_booked_sessions = db["sessions"].count_documents({"status": {"$in": ["pending", "confirmed", "completed"]}})
    pending_sessions = db["sessions"].count_documents({"status": "pending"})

    contacts_series = _daily_series(db, "contacts", "created_at", start_date, days)
    assessments_series = _daily_series(db, "assessments", "created_at", start_date, days)
    payments_series = _daily_series(db, "payments", "paid_at", start_date, days, extra_match={"status": "paid"})

    recent_contacts = []
    for row in db["contacts"].find({}, {"_id": 0}).sort("created_at", DESCENDING).limit(20):
        recent_contacts.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "email": row.get("email"),
                "subject": row.get("subject"),
                "message": row.get("message"),
                "user_id": row.get("user_id"),
                "created_at": _safe_iso(row.get("created_at")),
            }
        )

    recent_assessments = []
    for row in db["assessments"].find({}, {"_id": 0, "id": 1, "user_id": 1, "status": 1, "created_at": 1}).sort("created_at", DESCENDING).limit(20):
        recent_assessments.append(
            {
                "id": row.get("id"),
                "user_id": row.get("user_id"),
                "status": row.get("status"),
                "created_at": _safe_iso(row.get("created_at")),
            }
        )

    recent_paid_payments = []
    for row in db["payments"].find({"status": "paid"}, {"_id": 0, "id": 1, "user_id": 1, "assessment_id": 1, "amount": 1, "currency": 1, "order_id": 1, "paid_at": 1}).sort("paid_at", DESCENDING).limit(20):
        recent_paid_payments.append(
            {
                "id": row.get("id"),
                "user_id": row.get("user_id"),
                "assessment_id": row.get("assessment_id"),
                "amount": row.get("amount"),
                "currency": row.get("currency"),
                "order_id": row.get("order_id"),
                "paid_at": _safe_iso(row.get("paid_at")),
            }
        )

    recent_sessions = []
    for row in db["sessions"].find({}, {"_id": 0}).sort("created_at", DESCENDING).limit(20):
        recent_sessions.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "email": row.get("email"),
                "status": row.get("status"),
                "slot": row.get("slot"),
                "created_at": _safe_iso(row.get("created_at")),
            }
        )

    subject_pipeline = [
        {"$match": {"subject": {"$exists": True, "$ne": None, "$ne": ""}}},
        {"$group": {"_id": "$subject", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 8},
    ]
    top_contact_subjects = [{"subject": row["_id"], "count": int(row["count"])} for row in db["contacts"].aggregate(subject_pipeline)]

    active_users = set()
    for collection, field in [("assessments", "created_at"), ("payments", "paid_at"), ("contacts", "created_at")]:
        cursor = db[collection].find({field: {"$gte": active_since}}, {"_id": 0, "user_id": 1})
        for row in cursor:
            user_id = row.get("user_id")
            if isinstance(user_id, int):
                active_users.add(user_id)

    return {
        "window_days": days,
        "kpis": {
            "total_users": total_users,
            "active_users_30d": len(active_users),
            "total_contacts": total_contacts,
            "total_assessments": total_assessments,
            "total_recommendations": total_recommendations,
            "total_paid_payments": total_paid_payments,
            "total_sessions": total_sessions,
            "booked_sessions": total_booked_sessions,
            "pending_sessions": pending_sessions,
        },
        "series": {
            "contacts": contacts_series,
            "assessments": assessments_series,
            "paid_payments": payments_series,
        },
        "top_contact_subjects": top_contact_subjects,
        "recent": {
            "contacts": recent_contacts,
            "assessments": recent_assessments,
            "paid_payments": recent_paid_payments,
            "sessions": recent_sessions,
        },
    }
