from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptResult:
    generation_prompt: str  # English prompt for the image/video provider
    caption_text: str  # Invite copy in the user's preferred language


@runtime_checkable
class ClaudeAdapter(Protocol):
    async def build_prompt(self, input_payload: dict, language: str) -> PromptResult: ...


class ClaudePromptAdapter:
    """
    Uses claude-sonnet to turn vernacular customer input into:
      - A clean generation prompt (English) for the image/video provider
      - Invite caption copy in the user's language
    """

    _SYSTEM = (
        "You are a creative assistant for a wedding and life-events content app "
        "serving rural India. Given JSON customer input and a language code, "
        "produce a JSON response with exactly two keys: "
        '"generation_prompt" (concise English prompt for an AI image generator, '
        "≤120 words) and "
        '"caption_text" (wedding invite or event caption in the requested language, '
        "≤80 words). "
        "Be culturally appropriate and celebratory. Never include real celebrity names."
    )

    def __init__(self, api_key: str) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key)

    async def build_prompt(self, input_payload: dict, language: str) -> PromptResult:
        import json

        user_message = (
            f"Language: {language}\n"
            f"Customer input: {json.dumps(input_payload, ensure_ascii=False)}"
        )
        response = await self._client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=self._SYSTEM,
            messages=[{"role": "user", "content": user_message}],
        )
        raw_text = response.content[0].text
        try:
            import re

            # Strip markdown code fences if present
            cleaned = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw_text.strip())
            parsed = json.loads(cleaned)
            return PromptResult(
                generation_prompt=str(parsed["generation_prompt"]),
                caption_text=str(parsed["caption_text"]),
            )
        except Exception:
            logger.warning("ClaudePromptAdapter parse error, using raw text as prompt")
            return PromptResult(generation_prompt=raw_text[:500], caption_text="")


class FakeClaudeAdapter:
    """Deterministic fake for tests — returns predictable values without API calls."""

    async def build_prompt(self, input_payload: dict, language: str) -> PromptResult:
        return PromptResult(
            generation_prompt=f"Fake generation prompt for {input_payload}",
            caption_text=f"Fake caption in {language}",
        )
