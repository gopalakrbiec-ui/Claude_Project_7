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
            "otp": code,
            "sender": self._sender_id,
        }

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(
                    self._BASE_URL,
                    params=params,
                    headers={"authkey": self._auth_key},
                )
                resp.raise_for_status()
                logger.info("MSG91 OTP sent to %s", phone)
            except httpx.HTTPStatusError as e:
                logger.error("MSG91 OTP failed for %s: %s %s", phone, e.response.status_code, e.response.text)
                raise RuntimeError(f"OTP delivery failed: {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error("MSG91 OTP network error for %s: %s", phone, e)
                raise RuntimeError(f"OTP delivery network error: {e}") from e


class TwilioOtpAdapter:
    """
    Production SMS delivery via Twilio.
    No DLT registration required — works instantly in India for testing.

    Required env vars:
        TWILIO_ACCOUNT_SID  — starts with AC...
        TWILIO_AUTH_TOKEN   — from Twilio dashboard
        TWILIO_FROM_NUMBER  — your Twilio phone number e.g. +15551234567
    """

    def __init__(self, account_sid: str, auth_token: str, from_number: str) -> None:
        self._account_sid = account_sid
        self._auth_token = auth_token
        self._from_number = from_number
        self._url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"

    async def send_otp(self, phone: str, code: str) -> None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(
                    self._url,
                    auth=(self._account_sid, self._auth_token),
                    data={
                        "From": self._from_number,
                        "To": phone,
                        "Body": f"Your WeddingApp OTP is {code}. Valid for 5 minutes.",
                    },
                )
                resp.raise_for_status()
                logger.info("Twilio OTP sent to %s", phone)
            except httpx.HTTPStatusError as e:
                logger.error("Twilio OTP failed for %s: %s %s", phone, e.response.status_code, e.response.text)
                raise RuntimeError(f"OTP delivery failed: {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error("Twilio OTP network error for %s: %s", phone, e)
                raise RuntimeError(f"OTP delivery network error: {e}") from e
