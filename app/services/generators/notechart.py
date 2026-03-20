"""
Note chart generator.
Uses Claude Sonnet to generate active recall questions from the learning asset.
"""

import logging
import anthropic
from app.config import settings
from app.services.r2 import download_from_r2, upload_text_to_r2

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 8000


async def generate_notechart(topic_id: str, supabase_client) -> str:
    """Generate note chart questions from the learning asset."""
    # Get topic info
    topic_result = supabase_client.table("topics").select(
        "id, learning_asset_url"
    ).eq("id", topic_id).execute()
    topic = topic_result.data[0]

    if not topic.get("learning_asset_url"):
        raise ValueError(f"No learning asset found for topic {topic_id}")

    # Download learning asset from R2
    learning_asset = download_from_r2(topic["learning_asset_url"]).decode("utf-8")
    logger.info(f"Note chart [{topic_id}] — loaded learning asset ({len(learning_asset)} chars)")

    # Load base prompt
    prompt_result = supabase_client.table("base_prompts").select(
        "id, content"
    ).eq("feature", "notechart").eq("is_active", True).limit(1).execute()

    if not prompt_result.data:
        raise ValueError("No active prompt found for feature 'notechart'")

    base_prompt = prompt_result.data[0]["content"]

    # Call Sonnet (streaming to avoid timeout on long requests)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    chunks = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": f"{base_prompt}\n\n---\n\nLEARNING ASSET:\n\n{learning_asset}"
        }]
    ) as stream:
        for text in stream.text_stream:
            chunks.append(text)

    notechart_text = "".join(chunks)
    logger.info(f"Note chart [{topic_id}] — Sonnet returned {len(notechart_text)} chars")

    # Store on R2
    r2_key = f"{topic_id}/notechart.json"
    upload_text_to_r2(r2_key, notechart_text)
    logger.info(f"Note chart [{topic_id}] — stored on R2 at {r2_key}")

    # Update topic row
    supabase_client.table("topics").update({
        "notechart_url": r2_key
    }).eq("id", topic_id).execute()

    return r2_key
