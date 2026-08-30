from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.retry import HardTimeoutError, retrying_anthropic, with_timeout

logger = logging.getLogger(__name__)

# Default limits — overridden by config if wired through settings
_DEFAULT_TIMEOUT = 30.0   # seconds; moderation must be fast
_DEFAULT_RETRIES = 3


@dataclass(frozen=True)
class ModerationResult:
    allowed: bool
    reason: str           # human-readable; stored on rejected jobs for audit
    raw_response: dict    # full API response for audit storage


@runtime_checkable
class ModerationAdapter(Protocol):
    async def moderate(self, content: str) -> ModerationResult: ...


class ClaudeModerationAdapter:
    """
    Uses claude-haiku (cheapest/fastest) to check for:
      - Real public figures / named celebrities
      - NSFW content

    Retries: up to max_retries on APIConnectionError and 5xx/529 status.
    Timeout: hard wall-clock limit (default 30 s); moderation must finish
             before any paid generation step so we keep it tight.
    """

    _SYSTEM = (
        "You are a content moderation assistant for a wedding and life-events app "
        "serving customers across India. Respond ONLY with a JSON object with keys: "
        '"allowed" (bool), "reason" (string). '
        "Block content that: (1) depicts or names a real public figure, politician, "
        "celebrity, or identifiable living person by name; (2) contains or requests "
        "NSFW, sexual, violent, or hateful content. "
        "Allow: generic wedding themes, blessings, decorations, fictional people, "
        "event invitations."
    )

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = _DEFAULT_RETRIES,
        timeout_seconds: float = _DEFAULT_TIMEOUT,
    ) -> None:
        from anthropic import AsyncAnthropic

        # max_retries=0: we control retries ourselves via retrying_anthropic()
        self._client = AsyncAnthropic(api_key=api_key, max_retries=0)
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds

    async def moderate(self, content: str) -> ModerationResult:
        async def _call() -> ModerationResult:
            response = await self._client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                system=self._SYSTEM,
                messages=[{"role": "user", "content": content}],
            )
            raw_text = response.content[0].text
            try:
                parsed = json.loads(raw_text)
                allowed: bool = bool(parsed.get("allowed", False))
                reason: str = str(parsed.get("reason", ""))
            except (json.JSONDecodeError, AttributeError):
                # Conservative: treat unparseable response as blocked
                logger.warning(
                    "Moderation response unparseable, blocking by default: %r", raw_text
                )
                allowed = False
                reason = f"Moderation parse error: {raw_text[:200]}"
                parsed = {"raw": raw_text}
            return ModerationResult(allowed=allowed, reason=reason, raw_response=parsed)

        async def _with_retries() -> ModerationResult:
            return await retrying_anthropic(
                _call,
                max_attempts=self._max_retries,
                label="ClaudeModerationAdapter",
            )

        try:
            return await with_timeout(
                _with_retries(),
                seconds=self._timeout_seconds,
                label="ClaudeModerationAdapter",
            )
        except HardTimeoutError:
            logger.error(
                "Moderation timed out after %.0fs — blocking by default", self._timeout_seconds
            )
            return ModerationResult(
                allowed=False,
                reason=f"Moderation timeout after {self._timeout_seconds:.0f}s",
                raw_response={"error": "timeout"},
            )


class FakeModerationAdapter:
    """
    Deterministic fake for tests.
    Blocks if the string '__BLOCK__' appears anywhere in the content.
    """

    async def moderate(self, content: str) -> ModerationResult:
        if "__BLOCK__" in content:
            return ModerationResult(
                allowed=False,
                reason="Fake block: __BLOCK__ sentinel detected",
                raw_response={"allowed": False, "reason": "test block"},
            )
        return ModerationResult(
            allowed=True,
            reason="",
            raw_response={"allowed": True, "reason": ""},
        )
