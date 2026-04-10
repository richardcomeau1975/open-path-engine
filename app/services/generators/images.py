"""
Image generator.
Uses OpenAI GPT Image to generate visual overview images from prompts.
"""

import json
import base64
import logging
import httpx
from app.config import settings
from app.services.r2 import download_from_r2, upload_bytes_to_r2

logger = logging.getLogger(__name__)


IMAGE_STYLE = (
    "Generate an image in exactly the style of the reference image. "
    "Warm editorial illustration. Expressive ink line art with naturalistic color. "
    "Cream background. Generous negative space above for text overlay. "
    "No text or labels in the image."
)

_cached_reference_image = None

def _get_reference_image_b64() -> str:
    """Download and cache the style reference image from R2."""
    global _cached_reference_image
    if _cached_reference_image is None:
        try:
            img_bytes = download_from_r2("editorial_illustration.jpeg")
            _cached_reference_image = base64.b64encode(img_bytes).decode("utf-8")
        except Exception:
            _cached_reference_image = ""
    return _cached_reference_image


async def generate_images(topic_id: str, supabase_client) -> list[str]:
    """
    Generate images for the visual overview.

    1. Download visual_overview_script.json from R2
    2. Parse out image_prompt for each slide
    3. Call OpenAI image generation for each
    4. Store images on R2
    5. Update topic row with image URL list

    Returns list of R2 keys for the generated images.
    """
    # Get topic info
    topic_result = supabase_client.table("topics").select(
        "id, visual_overview_script_url"
    ).eq("id", topic_id).execute()
    topic = topic_result.data[0]

    if not topic.get("visual_overview_script_url"):
        raise ValueError(f"No visual overview script found for topic {topic_id}")

    # Download and parse the visual overview script
    script_raw = download_from_r2(topic["visual_overview_script_url"]).decode("utf-8")
    logger.info(f"Images [{topic_id}] — loaded visual overview script ({len(script_raw)} chars)")

    # The script might have markdown code fences around the JSON — strip them
    script_clean = script_raw.strip()
    if script_clean.startswith("```"):
        # Remove opening fence (possibly with language tag)
        first_newline = script_clean.index("\n")
        script_clean = script_clean[first_newline + 1:]
    if script_clean.endswith("```"):
        script_clean = script_clean[:-3]
    script_clean = script_clean.strip()

    try:
        slides = json.loads(script_clean)
    except json.JSONDecodeError as e:
        logger.error(f"Images [{topic_id}] — failed to parse visual overview script as JSON: {e}")
        logger.error(f"Images [{topic_id}] — raw content starts with: {script_raw[:200]}")
        raise ValueError(f"Visual overview script is not valid JSON: {e}")

    image_keys = []

    for slide in slides:
        slide_num = slide.get("slide_number", len(image_keys) + 1)
        image_prompt = slide.get("image_prompt", "")

        if not image_prompt:
            logger.warning(f"Images [{topic_id}] — slide {slide_num} has no image_prompt, skipping")
            continue

        logger.info(f"Images [{topic_id}] — generating image for slide {slide_num}")

        # Call OpenAI image generation
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "gpt-image-1",
                    "prompt": f"{IMAGE_STYLE} Scene: {image_prompt}",
                    "n": 1,
                    "size": "1536x1024",
                    "quality": "medium",
                }
            )

        if response.status_code != 200:
            logger.error(f"Images [{topic_id}] — OpenAI error for slide {slide_num}: {response.status_code} {response.text}")
            raise ValueError(f"OpenAI image generation failed for slide {slide_num}: {response.status_code}")

        result = response.json()

        # GPT Image returns base64-encoded image data
        image_b64 = result["data"][0].get("b64_json")
        if not image_b64:
            # Fallback: might return a URL instead
            image_url = result["data"][0].get("url")
            if image_url:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    img_response = await client.get(image_url)
                image_bytes = img_response.content
            else:
                raise ValueError(f"No image data returned for slide {slide_num}")
        else:
            image_bytes = base64.b64decode(image_b64)

        # Store on R2
        r2_key = f"{topic_id}/images/slide_{slide_num}.png"
        upload_bytes_to_r2(r2_key, image_bytes, content_type="image/png")
        image_keys.append(r2_key)
        logger.info(f"Images [{topic_id}] — stored slide {slide_num} on R2 ({len(image_bytes)} bytes)")

    # Update topic row with image list
    supabase_client.table("topics").update({
        "visual_overview_images": image_keys
    }).eq("id", topic_id).execute()

    logger.info(f"Images [{topic_id}] — generated {len(image_keys)} images")
    return image_keys
