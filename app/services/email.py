import smtplib
from email.message import EmailMessage

from app.core.config import get_settings


settings = get_settings()


def _smtp_ready() -> bool:
    return bool(settings.EMAIL_HOST and settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD)


def send_email(to_email: str, subject: str, body: str, attachments: list[tuple[str, bytes, str]] | None = None) -> None:
    if not _smtp_ready():
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{settings.EMAIL_FROM_NAME} <{settings.EMAIL_HOST_USER}>"
    msg["To"] = to_email
    msg.set_content(body)

    if attachments:
        for filename, data, mime_type in attachments:
            maintype, subtype = mime_type.split("/", 1)
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)

    with smtplib.SMTP(settings.EMAIL_HOST, settings.EMAIL_PORT) as server:
        if settings.EMAIL_USE_TLS:
            server.starttls()
        server.login(settings.EMAIL_HOST_USER, settings.EMAIL_HOST_PASSWORD)
        server.send_message(msg)
