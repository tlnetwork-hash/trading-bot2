"""Alerts: phone push notifications through ntfy, and/or email sent through
Brevo's web API or Gmail (SMTP).

Many cloud providers (DigitalOcean included) block the ports Gmail uses for
sending mail. ntfy and Brevo are reached over normal HTTPS, so they work there.
"""

import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

log = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"


# ntfy priorities: min, low, default, high, urgent. Low skips the phone's sound.
PRIORITIES = ("min", "low", "default", "high", "urgent")


class Notifier:
    def __init__(self, secrets, prefix, ntfy_url=""):
        self.secrets = secrets
        self.prefix = prefix
        self.ntfy_url = ntfy_url

    @property
    def channels(self):
        return [c for c, on in (("ntfy", self.ntfy_url), ("email", self.secrets.enabled)) if on]

    def send(self, subject, body, priority="default"):
        """Send an alert on every configured channel. Returns True if at least
        one channel accepted it."""
        if not self.channels:
            log.info("alerts not configured, skipping: %s", subject)
            return False
        delivered = False
        if self.ntfy_url:
            delivered |= self._attempt("ntfy", self._send_ntfy, subject, body, priority)
        if self.secrets.enabled:
            send = self._send_brevo if self.secrets.method == "brevo" else self._send_gmail
            delivered |= self._attempt(self.secrets.method, send, f"[{self.prefix}] {subject}", body)
        return delivered

    def _attempt(self, name, send, *args):
        try:
            send(*args)
            log.info("sent via %s: %s", name, args[0])
            return True
        except Exception:
            # A broken alert setup must never stop the bot from trading safely.
            log.exception("could not send alert via %s: %s", name, args[0])
            return False

    def _send_ntfy(self, subject, body, priority):
        title = f"{self.prefix}: {subject}"
        request = urllib.request.Request(
            self.ntfy_url,
            data=body.encode(),
            headers={
                # HTTP headers must be plain ASCII
                "Title": title.encode("ascii", "replace").decode(),
                "Priority": priority if priority in PRIORITIES else "default",
                "Tags": {"urgent": "rotating_light", "high": "warning"}.get(priority, "chart_with_upwards_trend"),
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15):
            pass

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
