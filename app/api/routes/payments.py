from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlalchemy.orm import Session

from app.api.deps.auth import get_current_user, get_db
from app.core.config import get_settings
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
from app.services.razorpay import create_order, verify_signature
from app.services.email import send_email


router = APIRouter(prefix="/payments", tags=["payments"])
settings = get_settings()


def _latest_paid_payment(db: Session, user_id: int) -> Payment | None:
    return (
        db.query(Payment)
        .filter(Payment.user_id == user_id, Payment.status == "paid")
        .order_by(Payment.paid_at.desc())
        .first()
    )


def _latest_recommendation(db: Session, user_id: int) -> Recommendation | None:
    return (
        db.query(Recommendation)
        .filter(Recommendation.user_id == user_id)
        .order_by(Recommendation.created_at.desc())
        .first()
    )


@router.post("/order", response_model=PaymentOrderResponse)
def create_payment_order(
    payload: PaymentOrderRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        order = create_order(payload.amount_inr)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    record = Payment(
        user_id=current_user.id,
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

    try:
        verify_signature(payload.order_id, payload.payment_id, payload.signature)
    except Exception:
        raise HTTPException(status_code=400, detail="Payment verification failed")

    payment.payment_id = payload.payment_id
    payment.signature = payload.signature
    payment.status = "paid"
    payment.paid_at = datetime.utcnow()
    db.commit()

    # Payment confirmation email
    confirmation_body = (
        f"Hi {current_user.name},\n\n"
        "Your payment was successful and your CareerSpark report is unlocked.\n\n"
        f"Order ID: {payment.order_id}\n"
        f"Payment ID: {payment.payment_id}\n"
        f"Amount: {payment.amount / 100:.2f} {payment.currency}\n"
        f"Paid at: {payment.paid_at.isoformat()}\n\n"
        "Thank you for choosing CareerSpark!"
    )
    background_tasks.add_task(
        send_email,
        current_user.email,
        "Payment successful - CareerSpark",
        confirmation_body,
    )

    # Report email
    latest = _latest_recommendation(db, current_user.id)
    if latest:
        report = latest.output_data or {}
        top_branches = report.get("top_branches", [])
        branch_lines = []
        for idx, branch in enumerate(top_branches, start=1):
            branch_lines.append(f"{idx}. {branch.get('branch')} - {branch.get('why_fit')}")
        report_body = (
            f"Hi {current_user.name},\n\n"
            "Here is your CareerSpark report summary:\n\n"
            f"Summary: {report.get('summary', 'N/A')}\n\n"
            "Top branches:\n"
            + "\n".join(branch_lines)
            + "\n\nNext steps:\n"
            + "\n".join([f"- {item}" for item in report.get("next_steps", [])])
            + "\n\nLog in to view the full report in your dashboard."
        )
        background_tasks.add_task(
            send_email,
            current_user.email,
            "Your CareerSpark report is ready",
            report_body,
        )

    return PaymentVerifyResponse(
        paid=True,
        order_id=payment.order_id,
        payment_id=payment.payment_id,
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


