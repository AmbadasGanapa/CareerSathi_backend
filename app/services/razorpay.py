from app.core.config import get_settings

try:
    import razorpay
except Exception:  # pragma: no cover
    razorpay = None


settings = get_settings()


def _client():
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay keys not configured")
    if razorpay is None:
        raise RuntimeError("razorpay package is not installed")
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def create_order(amount_inr: int) -> dict:
    client = _client()
    order = client.order.create({
        "amount": amount_inr * 100,
        "currency": "INR",
        "payment_capture": 1,
    })
    return order


def verify_signature(order_id: str, payment_id: str, signature: str) -> None:
    client = _client()
    payload = {
        "razorpay_order_id": order_id,
        "razorpay_payment_id": payment_id,
        "razorpay_signature": signature,
    }
    client.utility.verify_payment_signature(payload)
