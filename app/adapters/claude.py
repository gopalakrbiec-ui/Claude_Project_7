from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.retry import retrying_anthropic, with_timeout

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 60.0   # seconds — prompt building is less time-sensitive
_DEFAULT_RETRIES = 3


@dataclass(frozen=True)
class PromptResult:
    generation_prompt: str   # English prompt for the image/video provider
    caption_text: str        # Invite copy in the user's preferred language


@runtime_checkable
class ClaudeAdapter(Protocol):
    async def build_prompt(self, input_payload: dict, language: str) -> PromptResult: ...


class ClaudePromptAdapter:
    """
    Uses claude-sonnet to turn vernacular customer input into:
      - A clean generation prompt (English) for the image/video provider
      - Invite caption copy in the user's language

    Retries: up to max_retries on transient Anthropic errors.
    Timeout: hard wall-clock budget (default 60 s).

    Parse robustness: strips markdown code fences before JSON decode;
    falls back to using the raw text as the generation prompt if the
    response cannot be parsed as JSON (so we always return something
    usable rather than failing the entire job).
    """

    _SYSTEM = (
        "You are a creative assistant for a wedding and life-events photo app serving rural India. "
        "Given JSON customer input and a language code, produce a JSON response with exactly two keys:\n\n"
        '"generation_prompt": An English prompt for an AI image editor (≤120 words). '
        "Rules for the generation_prompt:\n"
        "  1. Start from the template scene_description as the base scene — always preserve it.\n"
        "  2. If the user provided a user_prompt (names, event details, custom requests), "
        "     incorporate those naturally into the scene.\n"
        "  3. Always end with: 'The person's face, skin tone, and facial features must be "
        "     preserved exactly from the uploaded photo. Do not alter the face in any way.'\n"
        "  4. ≤120 words total. Culturally appropriate. Never include real celebrity names.\n\n"
        '"caption_text": Wedding invite or event caption in the requested language (≤80 words). '
        "Celebratory and culturally appropriate."
    )

    def __init__(
        self,
        api_key: str,
        *,
        max_retries: int = _DEFAULT_RETRIES,
        timeout_seconds: float = _DEFAULT_TIMEOUT,
    ) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, max_retries=0)
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds

    async def build_prompt(self, input_payload: dict, language: str) -> PromptResult:
        user_message = (
            f"Language: {language}\n"
            f"Customer input: {json.dumps(input_payload, ensure_ascii=False)}"
        )

        async def _call() -> PromptResult:
            response = await self._client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=self._SYSTEM,
                messages=[{"role": "user", "content": user_message}],
            )
            raw_text = response.content[0].text
            return _parse_prompt_result(raw_text)

        async def _with_retries() -> PromptResult:
            return await retrying_anthropic(
                _call,
                max_attempts=self._max_retries,
                label="ClaudePromptAdapter",
            )

        return await with_timeout(
            _with_retries(),
            seconds=self._timeout_seconds,
            label="ClaudePromptAdapter",
        )


def _parse_prompt_result(raw_text: str) -> PromptResult:
    """Extract generation_prompt and caption_text from Claude's JSON response."""
    try:
        cleaned = re.sub(r"^```[a-z]*\n?|\n?```$", "", raw_text.strip())
        parsed = json.loads(cleaned)
        return PromptResult(
            generation_prompt=str(parsed["generation_prompt"]),
            caption_text=str(parsed["caption_text"]),
        )
    except Exception:
        logger.warning("ClaudePromptAdapter: parse error, using raw text as prompt")
        return PromptResult(generation_prompt=raw_text[:500], caption_text="")


class FakeClaudeAdapter:
    """Deterministic fake for tests — returns predictable values without API calls."""

    async def build_prompt(self, input_payload: dict, language: str) -> PromptResult:
        return PromptResult(
            generation_prompt=f"Fake generation prompt for {input_payload}",
            caption_text=f"Fake caption in {language}",
        )
