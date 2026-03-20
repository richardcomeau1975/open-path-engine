"""
Podcast audio generator.
Reads podcast script from R2, generates multi-speaker audio via Gemini TTS,
stores WAV on R2.
"""

import logging
from app.services.r2 import download_from_r2, upload_bytes_to_r2
from app.services.generators.tts import generate_multi_speaker_audio

logger = logging.getLogger(__name__)


async def generate_podcast_audio(topic_id: str, supabase_client) -> str:
    """
    Generate podcast audio from the podcast script.

    1. Download podcast script from R2
    2. Send to Gemini TTS (multi-speaker)
    3. Store WAV on R2
    4. Update topic row

    Returns the R2 key of the stored audio.
    """
    # Get topic info
    topic_result = supabase_client.table("topics").select(
        "id, podcast_script_url"
    ).eq("id", topic_id).execute()
    topic = topic_result.data[0]

    if not topic.get("podcast_script_url"):
        raise ValueError(f"No podcast script found for topic {topic_id}")

    # Download podcast script
    script_text = download_from_r2(topic["podcast_script_url"]).decode("utf-8")
    logger.info(f"Podcast audio [{topic_id}] — loaded script ({len(script_text)} chars)")

    # Check script length — Gemini TTS has input limits (~5000 chars recommended)
    # If the script is very long, we may need to chunk it later.
    # For now, if it exceeds 30000 chars, truncate with a warning.
    if len(script_text) > 30000:
        logger.warning(f"Podcast audio [{topic_id}] — script is {len(script_text)} chars, truncating to 30000")
        script_text = script_text[:30000]

    # Parse speaker names from the script
    # The podcast generator uses HOST: and EXPERT: labels
    # Detect the actual speaker names used
    speaker_a = "Host"
    speaker_b = "Expert"
    for line in script_text.split("\n")[:20]:
        line = line.strip()
        if ":" in line:
            name = line.split(":")[0].strip().upper()
            if name and len(name) < 30:
                if speaker_a == "Host":
                    speaker_a = line.split(":")[0].strip()
                elif line.split(":")[0].strip() != speaker_a:
                    speaker_b = line.split(":")[0].strip()
                    break

    logger.info(f"Podcast audio [{topic_id}] — detected speakers: {speaker_a}, {speaker_b}")

    # Generate audio
    wav_data = await generate_multi_speaker_audio(script_text, speaker_a, speaker_b)

    # Store on R2
    r2_key = f"{topic_id}/podcast_audio.wav"
    upload_bytes_to_r2(r2_key, wav_data, content_type="audio/wav")
    logger.info(f"Podcast audio [{topic_id}] — stored on R2 at {r2_key} ({len(wav_data)} bytes)")

    # Update topic row
    supabase_client.table("topics").update({
        "podcast_audio_url": r2_key
    }).eq("id", topic_id).execute()

    return r2_key
