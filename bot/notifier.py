"""Email alerts through Gmail (SMTP + app password)."""

import logging
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, secrets, prefix):
        self.secrets = secrets
        self.prefix = prefix

    def send(self, subject, body):
        subject = f"[{self.prefix}] {subject}"
        if not self.secrets.enabled:
            log.info("email not configured, skipping: %s", subject)
            return
        msg = EmailMessage()
        msg["From"] = self.secrets.gmail_address
        msg["To"] = self.secrets.to_address
        msg["Subject"] = subject
        msg.set_content(body)
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
                smtp.login(self.secrets.gmail_address, self.secrets.gmail_app_password)
                smtp.send_message(msg)
            log.info("emailed: %s", subject)
        except Exception:
            # A broken mail setup must never stop the bot from trading safely.
            log.exception("could not send email: %s", subject)
