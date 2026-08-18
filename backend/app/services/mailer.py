"""Generic SMTP mailer.

Sends a plain-text Markdown email through the configured SMTP server, reading the
same ``smtp_*`` settings used by the storage report feature so a single delivery
path serves every report (storage, daily paper digest, monthly summary).
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


def send_mail(subject: str, content: str, to: str | None = None) -> dict[str, Any]:
    """Send a plain-text email; returns ``{ok, sent_to}`` or ``{ok: False, error}``."""
    host = str(settings.get("smtp_host", "")).strip()
    if not host:
        return {"ok": False, "error": "SMTP not configured"}

    port = settings.get_int("smtp_port", 465)
    user = str(settings.get("smtp_user", ""))
    password = str(settings.get("smtp_password", ""))
    sender = str(settings.get("smtp_from", "") or user or "paicc@localhost")
    recipient = (to or str(settings.get("smtp_to", ""))).strip()
    if not recipient:
        return {"ok": False, "error": "SMTP recipient (smtp_to) not configured"}

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(content)

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        with server:
            if user:
                server.login(user, password)
            server.send_message(msg)
    except Exception as exc:  # noqa: BLE001
        logger.exception("send_mail failed")
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "sent_to": recipient}
