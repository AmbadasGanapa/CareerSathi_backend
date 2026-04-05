import smtplib
from email.message import EmailMessage

from app.core.config import get_settings


settings = get_settings()


def _smtp_ready() -> bool:
    return bool(settings.EMAIL_HOST and settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD)


def send_email(to_email: str, subject: str, body: str) -> None:
    if not _smtp_ready():
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_HOST_USER}>"
    msg["To"] = to_email
    msg.set_content(body)

    with smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT) as server:
        if settings.EMAIL_USE_TLS:
            server.starttls()
        server.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
        server.send_message(msg)
