"""
Visual overview narration audio generator.
Reads visual overview script, generates per-slide narration via Gemini TTS (single-speaker),
stores WAV files on R2.
"""

import json
import logging
from app.services.r2 import download_from_r2, upload_bytes_to_r2
from app.services.generators.tts import generate_single_speaker_audio

logger = logging.getLogger(__name__)


async def generate_narration_audio(topic_id: str, supabase_client) -> list[str]:
    """
    Generate narration audio for each slide in the visual overview.

    1. Download visual_overview_script.json from R2
    2. For each slide, generate single-speaker audio from the narration text
    3. Store each WAV on R2
    4. Update topic row with audio URL list

    Returns list of R2 keys.
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
    logger.info(f"Narration audio [{topic_id}] — loaded visual overview script ({len(script_raw)} chars)")

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
        logger.error(f"Narration audio [{topic_id}] — failed to parse script as JSON: {e}")
        raise ValueError(f"Visual overview script is not valid JSON: {e}")

    audio_keys = []

    for slide in slides:
        slide_num = slide.get("slide_number", len(audio_keys) + 1)
        narration = slide.get("narration", "")

        if not narration:
            logger.warning(f"Narration audio [{topic_id}] — slide {slide_num} has no narration, skipping")
            continue

        logger.info(f"Narration audio [{topic_id}] — generating audio for slide {slide_num}")

        # Generate single-speaker audio
        wav_data = await generate_single_speaker_audio(
            f"Say the following in a warm, clear, educational tone: {narration}",
            voice_name="Kore"
        )

        # Store on R2
        r2_key = f"{topic_id}/narration/slide_{slide_num}.wav"
        upload_bytes_to_r2(r2_key, wav_data, content_type="audio/wav")
        audio_keys.append(r2_key)
        logger.info(f"Narration audio [{topic_id}] — stored slide {slide_num} ({len(wav_data)} bytes)")

    # Update topic row
    supabase_client.table("topics").update({
        "visual_overview_audio_urls": audio_keys
    }).eq("id", topic_id).execute()

    logger.info(f"Narration audio [{topic_id}] — generated {len(audio_keys)} audio segments")
    return audio_keys
