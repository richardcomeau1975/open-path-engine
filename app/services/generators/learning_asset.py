"""
Learning asset generator.
Uses Claude Opus to generate a learning asset from parsed course materials.
"""

import logging
import anthropic
from app.config import settings
from app.services.r2 import download_from_r2, upload_text_to_r2
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-20250514"
MAX_TOKENS = 16000


async def generate_learning_asset(topic_id: str, supabase_client, framework_type: str = None, student_id: str = None, course_id: str = None) -> str:
    """
    Generate a learning asset for a topic.

    1. Download parsed text from R2
    2. Load base prompt from base_prompts table (feature = 'learning_asset_generator')
    3. Call Opus
    4. Store result on R2
    5. Update topic row with URL

    Returns the R2 key of the stored learning asset.
    """
    # 1. Get topic info
    topic_result = supabase_client.table("topics").select(
        "id, parsed_text_url, course_id"
    ).eq("id", topic_id).execute()
    topic = topic_result.data[0]

    # 2. Download parsed text from R2
    parsed_text = download_from_r2(topic["parsed_text_url"]).decode("utf-8")
    logger.info(f"Learning asset [{topic_id}] — loaded parsed text ({len(parsed_text)} chars)")

    # 3. Load base prompt (framework-aware lookup)
    base_prompt = get_prompt_for_feature("learning_asset_generator", framework_type)
    logger.info(f"Learning asset [{topic_id}] — loaded base prompt ({len(base_prompt)} chars)")

    # 4. Assemble modifiers
    modifier_text = gather_modifiers(
        feature="learning_asset_generator",
        student_id=student_id,
        course_id=course_id,
        topic_id=topic_id,
    )

    if modifier_text:
        prompt = f"{base_prompt}\n\n---\n\nMODIFIERS:\n\n{modifier_text}\n\n---\n\nSOURCE MATERIAL:\n\n{parsed_text}"
    else:
        prompt = f"{base_prompt}\n\n---\n\nSOURCE MATERIAL:\n\n{parsed_text}"

    # Call Opus (streaming to avoid timeout on long requests)
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    chunks = []
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    ) as stream:
        for text in stream.text_stream:
            chunks.append(text)

    learning_asset_text = "".join(chunks)
    logger.info(f"Learning asset [{topic_id}] — Opus returned {len(learning_asset_text)} chars")

    # 5. Store on R2
    r2_key = f"{topic_id}/learning_asset.md"
    upload_text_to_r2(r2_key, learning_asset_text)
    logger.info(f"Learning asset [{topic_id}] — stored on R2 at {r2_key}")

    # 6. Update topic row
    supabase_client.table("topics").update({
        "learning_asset_url": r2_key
    }).eq("id", topic_id).execute()

    return r2_key
