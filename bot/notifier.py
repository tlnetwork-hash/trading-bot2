"""Email alerts, sent either through Brevo's web API or through Gmail (SMTP).

Many cloud providers (DigitalOcean included) block the ports Gmail uses for
sending mail. Brevo is reached over normal HTTPS, so it works there.
"""

import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

log = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


class Notifier:
    def __init__(self, secrets, prefix):
        self.secrets = secrets
        self.prefix = prefix

    def send(self, subject, body):
        """Send an alert. Returns True if it was accepted for delivery."""
        subject = f"[{self.prefix}] {subject}"
        method = self.secrets.method
        if method is None:
            log.info("email not configured, skipping: %s", subject)
            return False
        try:
            if method == "brevo":
                self._send_brevo(subject, body)
            else:
                self._send_gmail(subject, body)
            log.info("emailed: %s", subject)
            return True
        except Exception:
            # A broken mail setup must never stop the bot from trading safely.
            log.exception("could not send email via %s: %s", method, subject)
            return False

    def _send_brevo(self, subject, body):
        payload = {
            "sender": {"name": "Grid bot", "email": self.secrets.gmail_address},
            "to": [{"email": self.secrets.to_address}],
            "subject": subject,
            "textContent": body,
        }
        request = urllib.request.Request(
            BREVO_URL,
            data=json.dumps(payload).encode(),
            headers={"api-key": self.secrets.brevo_api_key, "content-type": "application/json",
                     "accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15):
                pass
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Brevo refused the email (HTTP {e.code}): {detail}") from None

    def _send_gmail(self, subject, body):
        msg = EmailMessage()
        msg["From"] = self.secrets.gmail_address
        msg["To"] = self.secrets.to_address
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.login(self.secrets.gmail_address, self.secrets.gmail_app_password)
            smtp.send_message(msg)
