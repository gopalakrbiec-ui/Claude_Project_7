from __future__ import annotations

"""
Tap-to-build prompt keyword groups — lets Flutter render collapsible
keyword-chip sections so users can construct a rich prompt without typing.
No auth required; this is static reference data.
"""

from fastapi import APIRouter

from app.core.prompt_keywords import get_keyword_groups_response

router = APIRouter(prefix="/prompts", tags=["prompts"])


@router.get("/keyword-groups", summary="Grouped keyword chips for tap-to-build prompts")
async def keyword_groups() -> dict:
    return {"groups": get_keyword_groups_response()}
