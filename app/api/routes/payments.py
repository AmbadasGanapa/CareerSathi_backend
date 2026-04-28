from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pymongo import DESCENDING
from pymongo.database import Database

from app.api.deps.auth import get_current_user, get_db
from app.api.routes.recommendations import generate_recommendation_from_payload
from app.core.config import get_settings
from app.db.mongo import get_database, get_next_id, to_public_document
from app.schemas.payment import (
    PaymentOrderRequest,
    PaymentOrderResponse,
    PaymentStatusResponse,
    PaymentVerifyRequest,
    PaymentVerifyResponse,
)
from app.schemas.recommendation import RecommendationInput
from app.services.email import send_email
from app.services.razorpay import create_order, verify_signature
from app.services.report_pdf import build_report_pdf


router = APIRouter(prefix="/payments", tags=["payments"])
settings = get_settings()


def _latest_paid_payment(db: Database, user_id: int) -> dict | None:
    return to_public_document(
        db["payments"].find_one(
            {"user_id": user_id, "status": "paid"},
            sort=[("paid_at", DESCENDING)],
        )
    )


def _generate_report_for_payment(assessment_id: int, user_id: int) -> None:
    db = get_database()
    try:
        assessment = to_public_document(db["assessments"].find_one({"id": assessment_id}))
        user = to_public_document(db["users"].find_one({"id": user_id}))
        if not assessment or not user:
            return

        payload_data = RecommendationInput(**assessment["input_data"])
        data = generate_recommendation_from_payload(payload_data)

        rec = {
            "id": get_next_id("recommendations", "recommendations"),
            "user_id": user_id,
            "input_data": assessment["input_data"],
            "output_data": data,
            "created_at": datetime.utcnow(),
        }
        db["recommendations"].insert_one(rec)
        db["assessments"].update_one(
            {"id": assessment_id},
            {"$set": {"recommendation_id": rec["id"], "status": "complete"}},
        )

        payment = to_public_document(
            db["payments"].find_one(
                {"assessment_id": assessment_id, "user_id": user_id},
                sort=[("created_at", DESCENDING)],
            )
        )
        order_id = payment.get("order_id") if payment else "N/A"
        payment_id = payment.get("payment_id") if payment else "N/A"
        amount_text = f"{payment.get('amount', 9):.2f} {payment.get('currency', 'INR')}" if payment else "9.00 INR"

        report_name = payload_data.name or user["name"]
        report_email = payload_data.email or user["email"]
        confirmation_body = (
            f"Hi {report_name},\n\n"
            "Your payment was successful and your A.GCareerSathi report is unlocked.\n\n"
            f"Order ID: {order_id}\n"
            f"Payment ID: {payment_id}\n"
            f"Amount: {amount_text}\n\n"
            "Thank you for choosing A.GCareerSathi!"
        )
        send_email(user["email"], "Payment successful - A.GCareerSathi", confirmation_body)

        top_branches = data.get("top_branches", [])
        branch_lines = []
        for idx, branch in enumerate(top_branches, start=1):
            branch_lines.append(f"{idx}. {branch.get('branch')} - {branch.get('why_fit')}")
        report_body = (
            f"Hi {report_name},\n\n"
            "Here is your A.GCareerSathi report summary:\n\n"
            f"Summary: {data.get('summary', 'N/A')}\n\n"
            "Top branches:\n"
            + "\n".join(branch_lines)
            + "\n\nNext steps:\n"
            + "\n".join([f"- {item}" for item in data.get("next_steps", [])])
            + "\n\nLog in to view the full report in your dashboard."
        )
        pdf_bytes = build_report_pdf(data, report_name, report_email, assessment_id)
        send_email(
            user["email"],
            "Your A.GCareerSathi report is ready",
            report_body,
            attachments=[("careerspark-report.pdf", pdf_bytes, "application/pdf")],
        )
    except Exception as exc:
        db["assessments"].update_one({"id": assessment_id}, {"$set": {"status": "failed"}})
        print(f"Report generation failed for assessment {assessment_id}: {exc}")


@router.post("/order", response_model=PaymentOrderResponse)
def create_payment_order(
    payload: PaymentOrderRequest,
    db: Database = Depends(get_db),
    current_user=Depends(get_current_user),
):
    assessment = to_public_document(db["assessments"].find_one({"id": payload.assessment_id}))
    if not assessment or assessment.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if assessment.get("status") != "pending_payment":
        raise HTTPException(status_code=400, detail="Assessment already processed")

    try:
        order = create_order(payload.amount_inr)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    record = {
        "id": get_next_id("payments", "payments"),
        "user_id": current_user["id"],
        "assessment_id": assessment["id"],
        "order_id": order["id"],
        "amount": int(payload.amount_inr),
        "amount_paise": int(order["amount"]),
        "currency": order.get("currency", "INR"),
        "status": "created",
        "payment_id": None,
        "signature": None,
        "created_at": datetime.utcnow(),
        "paid_at": None,
    }
    db["payments"].insert_one(record)

    return PaymentOrderResponse(
        order_id=order["id"],
        amount=order["amount"],
        currency=order["currency"],
        key_id=settings.RAZORPAY_KEY_ID,
    )


@router.post("/verify", response_model=PaymentVerifyResponse)
def verify_payment(
    payload: PaymentVerifyRequest,
    background_tasks: BackgroundTasks,
    db: Database = Depends(get_db),
    current_user=Depends(get_current_user),
):
    payment = to_public_document(db["payments"].find_one({"order_id": payload.order_id, "user_id": current_user["id"]}))
    if not payment:
        raise HTTPException(status_code=404, detail="Payment order not found")

    assessment = to_public_document(db["assessments"].find_one({"id": payment.get("assessment_id")})) if payment.get("assessment_id") else None
    if not assessment or assessment.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if payment.get("status") == "paid":
        return PaymentVerifyResponse(
            paid=True,
            order_id=payment["order_id"],
            payment_id=payment.get("payment_id") or payload.payment_id,
            report_ready=assessment.get("status") == "complete",
            assessment_status=assessment.get("status"),
        )

    try:
        verify_signature(payload.order_id, payload.payment_id, payload.signature)
    except Exception:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    db["payments"].update_one(
        {"id": payment["id"]},
        {
            "$set": {
                "payment_id": payload.payment_id,
                "signature": payload.signature,
                "status": "paid",
                "paid_at": datetime.utcnow(),
            }
        },
    )

    latest_assessment = to_public_document(db["assessments"].find_one({"id": assessment["id"]}))
    if latest_assessment and latest_assessment.get("status") != "complete":
        db["assessments"].update_one({"id": assessment["id"]}, {"$set": {"status": "processing"}})

    refreshed = to_public_document(db["assessments"].find_one({"id": assessment["id"]}))
    if refreshed and refreshed.get("status") == "processing":
        background_tasks.add_task(_generate_report_for_payment, assessment["id"], current_user["id"])

    return PaymentVerifyResponse(
        paid=True,
        order_id=payment["order_id"],
        payment_id=payload.payment_id,
        report_ready=refreshed.get("status") == "complete" if refreshed else False,
        assessment_status=refreshed.get("status") if refreshed else None,
    )


@router.get("/status", response_model=PaymentStatusResponse)
def payment_status(
    db: Database = Depends(get_db),
    current_user=Depends(get_current_user),
):
    paid = _latest_paid_payment(db, current_user["id"])
    if not paid:
        return PaymentStatusResponse(paid=False)

    paid_at = paid.get("paid_at")
    return PaymentStatusResponse(
        paid=True,
        order_id=paid.get("order_id"),
        payment_id=paid.get("payment_id"),
        paid_at=paid_at.isoformat() if isinstance(paid_at, datetime) else str(paid_at) if paid_at else None,
    )
