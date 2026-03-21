"""
Learning asset generator.
Uses Claude Opus via Batch API to generate a learning asset from parsed course materials.
"""

import logging
from app.services.r2 import download_from_r2, upload_text_to_r2
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-20250514"
MAX_TOKENS = 16384


async def build_learning_asset_prompt(topic_id: str, supabase_client, framework_type: str = None, student_id: str = None, course_id: str = None) -> str:
    """
    Build the assembled prompt for learning asset generation.
    Returns the full prompt string ready for the Batch API.
    """
    # Get topic info
    topic_result = supabase_client.table("topics").select(
        "id, parsed_text_url, course_id"
    ).eq("id", topic_id).execute()
    topic = topic_result.data[0]

    # Download parsed text from R2
    parsed_text = download_from_r2(topic["parsed_text_url"]).decode("utf-8")
    logger.info(f"Learning asset [{topic_id}] — loaded parsed text ({len(parsed_text)} chars)")

    # Load base prompt (framework-aware lookup)
    base_prompt = get_prompt_for_feature("learning_asset_generator", framework_type)
    logger.info(f"Learning asset [{topic_id}] — loaded base prompt ({len(base_prompt)} chars)")

    # Assemble modifiers
    modifier_text = gather_modifiers(
        feature="learning_asset_generator",
        student_id=student_id,
        course_id=course_id,
        topic_id=topic_id,
    )

    if modifier_text:
        return f"{base_prompt}\n\n---\n\nMODIFIERS:\n\n{modifier_text}\n\n---\n\nSOURCE MATERIAL:\n\n{parsed_text}"
    else:
        return f"{base_prompt}\n\n---\n\nSOURCE MATERIAL:\n\n{parsed_text}"


async def store_learning_asset_result(topic_id: str, supabase_client, result_text: str) -> str:
    """
    Store the learning asset result on R2 and update the topic row.
    Returns the R2 key.
    """
    r2_key = f"{topic_id}/learning_asset.md"
    upload_text_to_r2(r2_key, result_text)
    logger.info(f"Learning asset [{topic_id}] — stored on R2 at {r2_key} ({len(result_text)} chars)")

    supabase_client.table("topics").update({
        "learning_asset_url": r2_key
    }).eq("id", topic_id).execute()

    return r2_key
