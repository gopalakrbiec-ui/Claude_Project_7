from __future__ import annotations

import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

logger = logging.getLogger(__name__)


class EmailAdapter:
    """
    Sends transactional email.

    Provider priority:
      1. Resend API  (RESEND_API_KEY set)
      2. SMTP        (SMTP_HOST set)
      3. Console log (dev fallback — logs the email body, never sends)
    """

    def __init__(
        self,
        *,
        resend_api_key: str = "",
        smtp_host: str = "",
        smtp_port: int = 587,
        smtp_user: str = "",
        smtp_password: str = "",
        from_address: str = "noreply@savinenapu.in",
    ) -> None:
        self._resend_key = resend_api_key
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_user = smtp_user
        self._smtp_password = smtp_password
        self._from = from_address

    async def send(self, *, to: str, subject: str, html: str, text: str) -> None:
        if self._resend_key:
            await self._send_resend(to=to, subject=subject, html=html, text=text)
        elif self._smtp_host:
            await self._send_smtp(to=to, subject=subject, html=html, text=text)
        else:
            logger.warning(
                "EmailAdapter: no provider configured — logging email instead of sending.\n"
                "To: %s\nSubject: %s\nBody:\n%s",
                to, subject, text,
            )

    async def _send_resend(self, *, to: str, subject: str, html: str, text: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {self._resend_key}"},
                json={"from": self._from, "to": [to], "subject": subject, "html": html, "text": text},
            )
            if resp.status_code not in (200, 201):
                logger.error("Resend error %s: %s", resp.status_code, resp.text)
                resp.raise_for_status()
        logger.info("EmailAdapter: sent via Resend to=%s subject=%s", to, subject)

    async def _send_smtp(self, *, to: str, subject: str, html: str, text: str) -> None:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._from
        msg["To"] = to
        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html, "html"))

        import asyncio
        def _blocking_send() -> None:
            context = ssl.create_default_context()
            with smtplib.SMTP(self._smtp_host, self._smtp_port) as server:
                server.ehlo()
                server.starttls(context=context)
                if self._smtp_user:
                    server.login(self._smtp_user, self._smtp_password)
                server.sendmail(self._from, to, msg.as_string())

        await asyncio.to_thread(_blocking_send)
        logger.info("EmailAdapter: sent via SMTP to=%s subject=%s", to, subject)
