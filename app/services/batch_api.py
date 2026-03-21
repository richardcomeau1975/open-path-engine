"""
Anthropic Batch API service.
Submits multiple requests as a single batch and polls for results.
50% cost reduction compared to direct API calls.
"""

import asyncio
import logging
import anthropic
from app.config import settings

logger = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds between status checks


async def run_anthropic_batch(requests: list[dict]) -> dict:
    """
    Submit requests to Anthropic Batch API and wait for results.

    Args:
        requests: list of dicts with keys:
            - custom_id: string identifier (e.g., "learning_asset")
            - model: Claude model name
            - prompt: assembled prompt text
            - max_tokens: optional, defaults to 16384

    Returns:
        dict keyed by custom_id with values being the response text,
        or None if that request errored.
    """
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    batch_requests = []
    for req in requests:
        batch_requests.append({
            "custom_id": req["custom_id"],
            "params": {
                "model": req["model"],
                "max_tokens": req.get("max_tokens", 16384),
                "messages": [
                    {"role": "user", "content": req["prompt"]}
                ]
            }
        })

    # Submit batch (sync SDK call)
    ids = [r["custom_id"] for r in requests]
    logger.info(f"Batch API — submitting {len(batch_requests)} request(s): {ids}")
    batch = client.messages.batches.create(requests=batch_requests)
    batch_id = batch.id
    logger.info(f"Batch API — created batch {batch_id}")

    # Poll for completion
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        batch = client.messages.batches.retrieve(batch_id)
        counts = batch.request_counts
        logger.info(
            f"Batch API — {batch_id} status: {batch.processing_status} "
            f"(processing={counts.processing}, succeeded={counts.succeeded}, "
            f"errored={counts.errored})"
        )
        if batch.processing_status == "ended":
            break

    # Collect results
    results = {}
    for result in client.messages.batches.results(batch_id):
        if result.result.type == "succeeded":
            results[result.custom_id] = result.result.message.content[0].text
            logger.info(f"Batch API — {result.custom_id}: succeeded ({len(results[result.custom_id])} chars)")
        else:
            results[result.custom_id] = None
            error_info = getattr(result.result, "error", "unknown error")
            logger.error(f"Batch API — {result.custom_id}: FAILED — {error_info}")

    return results
