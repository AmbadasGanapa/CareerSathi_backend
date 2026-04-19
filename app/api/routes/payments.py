from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from app.api.deps.auth import get_current_user, get_db
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.assessment import Assessment
from app.models.payment import Payment
from app.models.recommendation import Recommendation
from app.models.user import User
from app.schemas.payment import (
    PaymentOrderRequest,
    PaymentOrderResponse,
    PaymentVerifyRequest,
    PaymentVerifyResponse,
    PaymentStatusResponse,
)
from app.schemas.recommendation import RecommendationInput
from app.services.razorpay import create_order, verify_signature
from app.services.email import send_email
from app.services.report_pdf import build_report_pdf
from app.api.routes.recommendations import generate_recommendation_from_payload


router = APIRouter(prefix="/payments", tags=["payments"])
settings = get_settings()


def _latest_paid_payment(db: Session, user_id: int) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.user_id == user_id, Payment.status == "paid")
        .order_by(Payment.paid_at.desc())
        .first()
    )


def _generate_report_for_payment(assessment_id: int, user_id: int) -> None:
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

        payment = (
            db.query(Payment)
            .filter(Payment.assessment_id == assessment_id, Payment.user_id == user_id)
            .order_by(Payment.created_at.desc())
            .first()
        )
        order_id = payment.order_id if payment else "N/A"
        payment_id = payment.payment_id if payment else "N/A"
        amount_text = f"{payment.amount / 100:.2f} {payment.currency}" if payment else "9.00 INR"

        report_name = payload_data.name or user.name
        report_email = payload_data.email or user.email
        confirmation_body = (
            f"Hi {report_name},\n\n"
            "Your payment was successful and your A.GCareerSathi report is unlocked.\n\n"
            f"Order ID: {order_id}\n"
            f"Payment ID: {payment_id}\n"
            f"Amount: {amount_text}\n\n"
            "Thank you for choosing A.GCareerSathi!"
        )
        send_email(user.email, "Payment successful - A.GCareerSathi", confirmation_body)



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
            user.email,
            "Your A.GCareerSathi report is ready",
            report_body,
            attachments=[("careerspark-report.pdf", pdf_bytes, "application/pdf")],
        )
    except Exception as exc:
        if assessment:
            assessment.status = "failed"
            db.commit()
        print(f"Report generation failed for assessment {assessment_id}: {exc}")
    finally:
        db.close()


@router.post("/order", response_model=PaymentOrderResponse)
def create_payment_order(
    payload: PaymentOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    assessment = db.get(Assessment, payload.assessment_id)
    if not assessment or assessment.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if assessment.status != "pending_payment":
        raise HTTPException(status_code=400, detail="Assessment already processed")

    try:
        order = create_order(payload.amount_inr)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    record = Payment(
        user_id=current_user.id,
        assessment_id=assessment.id,
        order_id=order["id"],
        amount=order["amount"],
        currency=order.get("currency", "INR"),
        status="created",
    )
    db.add(record)
    db.commit()

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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    payment = (
        db.query(Payment)
        .filter(Payment.order_id == payload.order_id, Payment.user_id == current_user.id)
        .first()
    )
    if not payment:
        raise HTTPException(status_code=404, detail="Payment order not found")

    assessment = db.get(Assessment, payment.assessment_id) if payment.assessment_id else None
    if not assessment or assessment.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if payment.status == "paid":
        return PaymentVerifyResponse(
            paid=True,
            order_id=payment.order_id,
            payment_id=payment.payment_id or payload.payment_id,
            report_ready=assessment.status == "complete",
            assessment_status=assessment.status,
        )

    try:
        verify_signature(payload.order_id, payload.payment_id, payload.signature)
    except Exception:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    payment.payment_id = payload.payment_id
    payment.signature = payload.signature
    payment.status = "paid"
    payment.paid_at = datetime.utcnow()

    if assessment.status != "complete":
        assessment.status = "processing"
    db.commit()

    if assessment.status == "processing":
        background_tasks.add_task(_generate_report_for_payment, assessment.id, current_user.id)

    return PaymentVerifyResponse(
        paid=True,
        order_id=payment.order_id,
        payment_id=payment.payment_id,
        report_ready=assessment.status == "complete",
        assessment_status=assessment.status,
    )


@router.get("/status", response_model=PaymentStatusResponse)
def payment_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    paid = _latest_paid_payment(db, current_user.id)
    if not paid:
        return PaymentStatusResponse(paid=False)

    return PaymentStatusResponse(
        paid=True,
        order_id=paid.order_id,
        payment_id=paid.payment_id,
        paid_at=paid.paid_at.isoformat() if paid.paid_at else None,
    )
