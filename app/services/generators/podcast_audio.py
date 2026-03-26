"""
Podcast audio generator.
Reads podcast script from R2, generates multi-speaker audio via Gemini TTS,
stores WAV on R2.
"""

import asyncio
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

    # Parse speaker names from the script
    speaker_a = "Host"
    speaker_b = "Expert"
    for line in script_text.split("\n")[:20]:
        line = line.strip()
        if ":" in line:
            name = line.split(":")[0].strip()
            if name and len(name) < 30 and not name.startswith("["):
                if speaker_a == "Host":
                    speaker_a = name
                elif name != speaker_a:
                    speaker_b = name
                    break

    logger.info(f"Podcast audio [{topic_id}] — detected speakers: {speaker_a}, {speaker_b}")

    # Chunk the script to avoid Gemini TTS ~10min output limit per call.
    # Split at natural speaker-line boundaries, ~8000 chars per chunk.
    CHUNK_SIZE = 8000
    chunks = _chunk_script(script_text, CHUNK_SIZE)
    logger.info(f"Podcast audio [{topic_id}] — split into {len(chunks)} chunks")

    # Generate audio for each chunk and concatenate
    all_pcm = bytearray()
    for i, chunk in enumerate(chunks):
        if i > 0:
            logger.info(f"Podcast audio [{topic_id}] — waiting 7s for TTS rate limit")
            await asyncio.sleep(7)
        logger.info(f"Podcast audio [{topic_id}] — generating chunk {i + 1}/{len(chunks)} ({len(chunk)} chars)")
        wav_data = await generate_multi_speaker_audio(chunk, speaker_a, speaker_b)
        # Extract PCM from WAV (skip 44-byte header)
        all_pcm.extend(wav_data[44:])

    # Wrap concatenated PCM in a single WAV
    import io, wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(bytes(all_pcm))
    wav_data = buf.getvalue()
    logger.info(f"Podcast audio [{topic_id}] — total audio: {len(wav_data)} bytes ({len(all_pcm) / 24000 / 2:.0f}s)")

    # Store on R2
    r2_key = f"{topic_id}/podcast_audio.wav"
    upload_bytes_to_r2(r2_key, wav_data, content_type="audio/wav")
    logger.info(f"Podcast audio [{topic_id}] — stored on R2 at {r2_key} ({len(wav_data)} bytes)")

    # Update topic row
    supabase_client.table("topics").update({
        "podcast_audio_url": r2_key
    }).eq("id", topic_id).execute()

    return r2_key


def _chunk_script(script_text: str, max_chars: int = 8000) -> list[str]:
    """
    Split a podcast script into chunks at speaker-line boundaries.
    Each chunk stays under max_chars. Preserves complete speaker turns.
    """
    lines = script_text.split("\n")
    chunks = []
    current_chunk = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current_len + line_len > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        current_chunk.append(line)
        current_len += line_len

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    # If script is short enough for one chunk, return as-is
    if not chunks:
        chunks = [script_text]

    return chunks
