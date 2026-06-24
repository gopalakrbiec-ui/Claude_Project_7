from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class OtpAdapter(Protocol):
    """
    Interface for one-time-password delivery.

    Implementations must be safe to call concurrently.  They should not raise
    on transient delivery failures — they should log and return so the caller
    can decide whether to surface the error.
    """

    async def send_otp(self, phone: str, code: str) -> None:
        """Deliver the OTP code to the given phone number."""
        ...


class ConsoleOtpAdapter:
    """
    Development implementation — prints OTP to stdout / logs it.
    Zero external dependencies; swap for a real SMS adapter in production
    by providing a different OtpAdapter implementation via dependency injection.
    """

    async def send_otp(self, phone: str, code: str) -> None:
        # Visible in `docker compose logs api` during local dev.
        logger.warning("DEV OTP  |  phone=%s  code=%s", phone, code)
        print(f"\n{'='*40}\nDEV OTP  phone={phone}  code={code}\n{'='*40}\n", flush=True)
