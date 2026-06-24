from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModerationResult:
    allowed: bool
    reason: str  # human-readable; stored on rejected jobs for audit
    raw_response: dict  # full API response for audit storage


@runtime_checkable
class ModerationAdapter(Protocol):
    async def moderate(self, content: str) -> ModerationResult: ...


class ClaudeModerationAdapter:
    """
    Uses claude-haiku (cheapest/fastest) to check for:
      - Real public figures / named celebrities
      - NSFW content
    Runs before any paid generation step.
    """

    _SYSTEM = (
        "You are a content moderation assistant for a wedding and life-events app "
        "serving rural India. Respond ONLY with a JSON object with keys: "
        '"allowed" (bool), "reason" (string). '
        "Block content that: (1) depicts or names a real public figure, politician, "
        "celebrity, or identifiable living person by name; (2) contains or requests "
        "NSFW, sexual, violent, or hateful content. "
        "Allow: generic wedding themes, blessings, decorations, fictional people, "
        "event invitations."
    )

    def __init__(self, api_key: str) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)

    async def moderate(self, content: str) -> ModerationResult:
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
            logger.warning("Moderation response unparseable, blocking by default: %r", raw_text)
            allowed = False
            reason = f"Moderation parse error: {raw_text[:200]}"
            parsed = {"raw": raw_text}

        return ModerationResult(allowed=allowed, reason=reason, raw_response=parsed)


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
