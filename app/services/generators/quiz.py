"""
Quiz generator.
Uses Claude Sonnet to generate multiple-choice questions from the learning asset.
Caches results on R2.
"""

import json
import logging
import anthropic
from app.config import settings
from app.services.r2 import download_from_r2, upload_text_to_r2
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6-20250220"
MAX_TOKENS = 8000


async def generate_quiz(topic_id: str, supabase_client, framework_type: str = None, student_id: str = None, course_id: str = None) -> list[dict]:
    """
    Generate quiz questions from the learning asset.
    Checks R2 for cached quiz first. If not found, generates and caches.
    Returns list of question objects.
    """
    r2_key = f"{topic_id}/quiz.json"

    # Check for cached quiz
    try:
        cached = download_from_r2(r2_key).decode("utf-8")
        clean = cached.strip()
        if clean.startswith("```"):
            clean = clean[clean.index("\n") + 1:]
        if clean.endswith("```"):
            clean = clean[:-3]
        questions = json.loads(clean.strip())
        logger.info(f"Quiz [{topic_id}] — loaded cached quiz ({len(questions)} questions)")
        return questions
    except Exception:
        logger.info(f"Quiz [{topic_id}] — no cached quiz, generating")

    # Get learning asset
    topic_result = supabase_client.table("topics").select(
        "id, learning_asset_url"
    ).eq("id", topic_id).execute()
    topic = topic_result.data[0]

    if not topic.get("learning_asset_url"):
        raise ValueError(f"No learning asset found for topic {topic_id}")

    learning_asset = download_from_r2(topic["learning_asset_url"]).decode("utf-8")
    logger.info(f"Quiz [{topic_id}] — loaded learning asset ({len(learning_asset)} chars)")

    # Load base prompt (framework-aware lookup)
    base_prompt = get_prompt_for_feature("quiz_generator", framework_type)

    # Assemble modifiers
    modifier_text = gather_modifiers(
        feature="quiz_generator",
        student_id=student_id,
        course_id=course_id,
        topic_id=topic_id,
    )

    if modifier_text:
        prompt = f"{base_prompt}\n\n---\n\nMODIFIERS:\n\n{modifier_text}\n\n---\n\nLEARNING ASSET:\n\n{learning_asset}"
    else:
        prompt = f"{base_prompt}\n\n---\n\nLEARNING ASSET:\n\n{learning_asset}"

    # Call Sonnet with streaming to avoid timeout
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    raw = ""
    with client.messages.stream(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{
            "role": "user",
            "content": prompt
        }]
    ) as stream:
        for text in stream.text_stream:
            raw += text

    logger.info(f"Quiz [{topic_id}] — Sonnet returned {len(raw)} chars")

    # Parse JSON
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean[clean.index("\n") + 1:]
    if clean.endswith("```"):
        clean = clean[:-3]
    clean = clean.strip()

    try:
        questions = json.loads(clean)
    except json.JSONDecodeError as e:
        logger.error(f"Quiz [{topic_id}] — failed to parse as JSON: {e}")
        raise ValueError(f"Quiz generation returned invalid JSON: {e}")

    # Cache on R2
    upload_text_to_r2(r2_key, json.dumps(questions, indent=2))
    logger.info(f"Quiz [{topic_id}] — cached {len(questions)} questions on R2")

    return questions
