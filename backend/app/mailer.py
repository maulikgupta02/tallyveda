"""Outgoing email (consent one-time passwords) over SMTP.

Configured with TC_SMTP_HOST/PORT/USER/PASSWORD/FROM. Port 465 uses implicit
TLS, anything else STARTTLS. Without SMTP settings in TC_DEV, messages are only
logged, so local runs and tests work without a mail server.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from . import config

log = logging.getLogger("tally_connector")

# Tests read what would have been sent.
outbox: list[EmailMessage] = []


class MailError(RuntimeError):
    pass


def send(to: str, subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["From"] = f"{config.PLATFORM_NAME} <{config.SMTP_FROM}>" if config.SMTP_FROM else config.PLATFORM_NAME
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    if not config.SMTP_HOST:
        if not config.DEV:
            raise MailError("Email is not configured on the server (TC_SMTP_HOST).")
        outbox.append(msg)
        log.warning("email to %s not sent (no SMTP configured): %s", to, subject)
        return
    try:
        if config.SMTP_PORT == 465:
            with smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ssl.create_default_context(), timeout=20) as s:
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(config.SMTP_USER, config.SMTP_PASSWORD)
                s.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        log.exception("sending email to %s failed", to)
        raise MailError("The verification email could not be sent. Please try again shortly.") from e
