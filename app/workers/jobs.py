from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def generate_content(ctx: dict, *, order_id: int) -> None:
    """
    Arq job: run the full generation pipeline for an order.

    Pipeline (implemented in future PRs):
      1. Load order from DB
      2. Set order.status = moderating
      3. Run safety gate (Claude moderation)
      4. If blocked: set status=rejected, refund credits, return
      5. Set order.status = generating
      6. Call generation adapter (image/video gen provider)
      7. Upload outputs to R2
      8. Set order.status = done, store output_keys
      9. Notify user (push notification)
    """
    logger.info("generate_content called for order_id=%s (stub — not yet implemented)", order_id)
