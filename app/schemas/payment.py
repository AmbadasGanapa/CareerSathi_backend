from pydantic import BaseModel


class PaymentOrderRequest(BaseModel):
    amount_inr: int = 9
    assessment_id: int


class PaymentOrderResponse(BaseModel):
    order_id: str
    amount: int
    currency: str
    key_id: str


class PaymentVerifyRequest(BaseModel):
    order_id: str
    payment_id: str
    signature: str


class PaymentVerifyResponse(BaseModel):
    paid: bool
    order_id: str
    payment_id: str
    report_ready: bool = False


class PaymentStatusResponse(BaseModel):
    paid: bool
    order_id: str | None = None
    payment_id: str | None = None
    paid_at: str | None = None
