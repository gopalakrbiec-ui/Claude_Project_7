from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import httpx

logger = logging.getLogger(__name__)


@runtime_checkable
class OtpAdapter(Protocol):
    async def send_otp(self, phone: str, code: str) -> None:
        ...


class ConsoleOtpAdapter:
    """Development only — prints OTP to logs."""

    async def send_otp(self, phone: str, code: str) -> None:
        logger.warning("DEV OTP  |  phone=%s  code=%s", phone, code)
        print(f"\n{'='*40}\nDEV OTP  phone={phone}  code={code}\n{'='*40}\n", flush=True)


class Msg91OtpAdapter:
    """
    Production SMS delivery via MSG91 (https://msg91.com).
    Uses the Send OTP API — one call sends and the template renders the code.

    Required env vars:
        MSG91_AUTH_KEY   — API key from MSG91 dashboard
        MSG91_TEMPLATE_ID — OTP template ID (create one in MSG91 dashboard)
        MSG91_SENDER_ID  — 6-char sender ID approved by MSG91 (default WDGAPP)
    """

    _BASE_URL = "https://control.msg91.com/api/v5/otp"

    def __init__(self, auth_key: str, template_id: str, sender_id: str = "WDGAPP") -> None:
        self._auth_key = auth_key
        self._template_id = template_id
        self._sender_id = sender_id

    async def send_otp(self, phone: str, code: str) -> None:
        # MSG91 expects phone without + prefix
        mobile = phone.lstrip("+")

        params = {
            "template_id": self._template_id,
            "mobile": mobile,
            "authkey": self._auth_key,
            "otp": code,
            "sender": self._sender_id,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(self._BASE_URL, params=params)
                resp.raise_for_status()
                logger.info("MSG91 OTP sent to %s", phone)
            except httpx.HTTPStatusError as e:
                logger.error("MSG91 OTP failed for %s: %s %s", phone, e.response.status_code, e.response.text)
            except httpx.RequestError as e:
                logger.error("MSG91 OTP network error for %s: %s", phone, e)
