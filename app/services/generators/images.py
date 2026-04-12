"""
Image generator.
Uses OpenAI Responses API with gpt-image-1.5 for style-consistent editorial illustrations.
Reference image on R2 defines the visual style via style transfer.
"""

import json
import base64
import logging
import httpx
from openai import OpenAI
from app.config import settings
from app.services.r2 import download_from_r2, upload_bytes_to_r2

logger = logging.getLogger(__name__)

# Cached reference image (base64 encoded)
_cached_reference_image = None


def _get_reference_image_b64() -> str:
    """Download and cache the style reference image from R2."""
    global _cached_reference_image
    if _cached_reference_image is None:
        try:
            img_bytes = download_from_r2("editorial_illustration.jpeg")
            _cached_reference_image = base64.b64encode(img_bytes).decode("utf-8")
            logger.info(f"Images — loaded reference image from R2 ({len(img_bytes)} bytes)")
        except Exception as e:
            logger.error(f"Images — failed to load reference image: {e}")
            _cached_reference_image = ""
    return _cached_reference_image


STYLE_PROMPT = (
    "STYLE INSTRUCTIONS — match the reference image precisely:\n"
    "Thin, confident ink line art — clean single-weight lines, NOT thick cartoon outlines. "
    "Minimal color: desaturated watercolor washes applied sparingly. Most of the image is line work with color only as accent. "
    "Background: near-white with barely perceptible warmth — NOT yellow, NOT cream, NOT tan. Think white paper with a hint of warmth. "
    "Proportions: naturalistic and anatomically correct — NOT exaggerated, NOT cartoonish, NOT children's book. "
    "The feeling is a sophisticated editorial illustration from The New Yorker or Monocle — restrained, elegant, understated. "
    "Generous negative space in the upper third for text overlay. "
    "No text, labels, captions, or words anywhere in the image. "
    "No logos, UI elements, or diagrams. "
    "AVOID: heavy saturation, thick outlines, exaggerated features, bright colors, busy compositions, cartoon proportions.\n\n"
)


async def generate_images(topic_id: str, supabase_client) -> list[str]:
    """
    Generate images for the visual overview / lecture segments.

    1. Download visual_overview_script.json from R2
    2. Parse out image_prompt for each slide
    3. Call OpenAI Responses API with reference image for style transfer
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

    # Strip markdown code fences if present
    script_clean = script_raw.strip()
    if script_clean.startswith("```"):
        first_newline = script_clean.index("\n")
        script_clean = script_clean[first_newline + 1:]
    if script_clean.endswith("```"):
        script_clean = script_clean[:-3]
    script_clean = script_clean.strip()

    try:
        slides = json.loads(script_clean)
    except json.JSONDecodeError as e:
        logger.error(f"Images [{topic_id}] — failed to parse visual overview script as JSON: {e}")
        raise ValueError(f"Visual overview script is not valid JSON: {e}")

    # Load reference image for style transfer
    ref_b64 = _get_reference_image_b64()
    if not ref_b64:
        logger.warning(f"Images [{topic_id}] — no reference image available, generating without style reference")

    image_keys = []
    client = OpenAI(api_key=settings.OPENAI_API_KEY)

    for slide in slides:
        slide_num = slide.get("slide_number", len(image_keys) + 1)
        image_prompt = slide.get("image_prompt", "")

        if not image_prompt:
            logger.warning(f"Images [{topic_id}] — slide {slide_num} has no image_prompt, skipping")
            continue

        logger.info(f"Images [{topic_id}] — generating image for slide {slide_num}: {image_prompt[:80]}...")

        full_prompt = f"{STYLE_PROMPT}SCENE TO ILLUSTRATE:\n{image_prompt}"

        try:
            # Build input with reference image for style transfer
            input_content = []

            # Add reference image if available
            if ref_b64:
                input_content.append({
                    "type": "input_image",
                    "image_url": f"data:image/jpeg;base64,{ref_b64}",
                })

            # Add the generation prompt
            input_content.append({
                "type": "input_text",
                "text": full_prompt,
            })

            response = client.responses.create(
                model="gpt-4.1",
                input=[{
                    "role": "user",
                    "content": input_content,
                }],
                tools=[{
                    "type": "image_generation",
                    "quality": "medium",
                    "size": "1536x1024",
                }],
            )

            # Extract image from response
            image_b64 = None
            for output in response.output:
                if output.type == "image_generation_call":
                    image_b64 = output.result
                    break

            if not image_b64:
                logger.error(f"Images [{topic_id}] — no image returned for slide {slide_num}")
                continue

            image_bytes = base64.b64decode(image_b64)

        except Exception as e:
            logger.error(f"Images [{topic_id}] — generation failed for slide {slide_num}: {e}")
            raise ValueError(f"Image generation failed for slide {slide_num}: {e}")

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
