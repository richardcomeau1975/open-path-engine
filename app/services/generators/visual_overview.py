"""
Visual overview script generator.
Uses Claude Sonnet via Batch API to generate narration + image prompts from the learning asset.
"""

import logging
from app.services.r2 import download_from_r2, upload_text_to_r2
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"
MAX_TOKENS = 8000


async def build_visual_overview_prompt(topic_id: str, supabase_client, learning_asset: str, framework_type: str = None, student_id: str = None, course_id: str = None) -> str:
    """
    Build the assembled prompt for visual overview script generation.
    Accepts the learning asset text directly (already downloaded by pipeline).
    """
    # Load base prompt (framework-aware lookup)
    base_prompt = get_prompt_for_feature("visual_overview", framework_type)

    # Assemble modifiers
    modifier_text = gather_modifiers(
        feature="visual_overview",
        student_id=student_id,
        course_id=course_id,
        topic_id=topic_id,
    )

    if modifier_text:
        return f"{base_prompt}\n\n---\n\nMODIFIERS:\n\n{modifier_text}\n\n---\n\nLEARNING ASSET:\n\n{learning_asset}"
    else:
        return f"{base_prompt}\n\n---\n\nLEARNING ASSET:\n\n{learning_asset}"


async def store_visual_overview_result(topic_id: str, supabase_client, result_text: str) -> str:
    """
    Store the visual overview script result on R2 and update the topic row.
    Returns the R2 key.
    """
    r2_key = f"{topic_id}/visual_overview_script.json"
    upload_text_to_r2(r2_key, result_text)
    logger.info(f"Visual overview [{topic_id}] — stored on R2 at {r2_key} ({len(result_text)} chars)")

    supabase_client.table("topics").update({
        "visual_overview_script_url": r2_key
    }).eq("id", topic_id).execute()

    return r2_key
