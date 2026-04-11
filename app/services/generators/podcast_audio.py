"""
Lecture audio generator.
Uses Inworld TTS (voice: Kelsey) with word-level timestamps.
Stores MP3 audio + timestamp JSON on R2.
"""

import asyncio
import base64
import json
import re
import logging
from app.services.r2 import download_from_r2, upload_text_to_r2, upload_bytes_to_r2
from app.services.generators.tts import inworld_tts

logger = logging.getLogger(__name__)


async def generate_podcast_audio(topic_id: str, supabase_client) -> str:
    """Generate lecture audio using Inworld TTS with word-level timestamps."""

    # Load script
    topic = supabase_client.table("topics").select("podcast_script_url").eq("id", topic_id).execute()
    if not topic.data or not topic.data[0].get("podcast_script_url"):
        raise Exception("No lecture script found")

    script_bytes = download_from_r2(topic.data[0]["podcast_script_url"])
    script = script_bytes.decode("utf-8")
    logger.info(f"Lecture audio [{topic_id}] — loaded script ({len(script)} chars)")

    # Extract anchor markers
    anchors = []
    for match in re.finditer(r'\[ANCHOR:\s*"([^"]+)"\]', script):
        anchors.append({"text": match.group(1), "char_position": match.start()})

    # Extract image prompts before cleaning
    image_prompts = []
    for match in re.finditer(r'\[IMAGE_PROMPT:\s*"([^"]+)"\]', script):
        image_prompts.append({"text": match.group(1), "char_position": match.start()})

    # Clean script for TTS — remove anchors and image prompts
    clean_script = re.sub(r'\[ANCHOR:\s*"[^"]+"\]', '', script)
    clean_script = re.sub(r'\[IMAGE_PROMPT:\s*"[^"]+"\]', '', clean_script)
    # Strip speaker labels — TTS should not read them aloud
    clean_script = re.sub(r'^(TEACHER|STUDENT_CHLOE|STUDENT_NATE|STUDENT_MIA):\s*', '', clean_script, flags=re.MULTILINE)

    # Chunk at paragraph boundaries
    paragraphs = clean_script.split("\n\n")
    chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > 2000 and current:
            chunks.append(current.strip())
            current = ""
        current += para + "\n\n"
    if current.strip():
        chunks.append(current.strip())

    logger.info(f"Lecture audio [{topic_id}] — {len(chunks)} chunks, {len(anchors)} anchors")

    # TTS each chunk
    all_audio_parts = []
    all_timestamps = []
    cumulative_time = 0.0

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(1)
        logger.info(f"Lecture audio [{topic_id}] — TTS chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")

        # Retry logic for TTS chunks
        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                result = await inworld_tts(chunk, voice_id="Kelsey", get_timestamps=True)
                break
            except Exception as e:
                if attempt < max_retries:
                    logger.warning(f"Lecture audio [{topic_id}] — TTS chunk {i+1}/{len(chunks)} failed (attempt {attempt+1}), retrying: {e}")
                    await asyncio.sleep(2)
                else:
                    raise

        if not result["audio"]:
            logger.error(f"Lecture audio [{topic_id}] — chunk {i+1} no audio")
            continue

        all_audio_parts.append(base64.b64decode(result["audio"]))

        if result["timestamps"]:
            words = result["timestamps"].get("words", [])
            starts = result["timestamps"].get("wordStartTimeSeconds", [])
            ends = result["timestamps"].get("wordEndTimeSeconds", [])
            for j in range(min(len(words), len(starts), len(ends))):
                all_timestamps.append({
                    "word": words[j],
                    "start": starts[j] + cumulative_time,
                    "end": ends[j] + cumulative_time,
                })
            if ends:
                cumulative_time = ends[-1] + cumulative_time + 0.1

    if not all_audio_parts:
        raise Exception("No audio generated")

    # Concatenate MP3 parts
    combined_audio = b"".join(all_audio_parts)

    # Match anchors to timestamps
    anchor_timings = []
    for anchor in anchors:
        anchor_words = anchor["text"].lower().split()
        if not anchor_words:
            continue
        matched = False
        for i, ts in enumerate(all_timestamps):
            if ts["word"].lower().strip(".,!?;:\"'") == anchor_words[0].strip(".,!?;:\"'"):
                match = True
                for k, aw in enumerate(anchor_words[1:], 1):
                    if i + k >= len(all_timestamps):
                        match = False
                        break
                    if all_timestamps[i + k]["word"].lower().strip(".,!?;:\"'") != aw.strip(".,!?;:\"'"):
                        match = False
                        break
                if match:
                    end_idx = min(i + len(anchor_words) - 1, len(all_timestamps) - 1)
                    anchor_timings.append({
                        "text": anchor["text"],
                        "start_time": ts["start"],
                        "end_time": all_timestamps[end_idx]["end"],
                    })
                    matched = True
                    break
        if not matched:
            total_chars = sum(len(ts["word"]) + 1 for ts in all_timestamps)
            if total_chars > 0 and all_timestamps:
                ratio = anchor["char_position"] / max(total_chars, 1)
                est_time = ratio * all_timestamps[-1]["end"]
                anchor_timings.append({
                    "text": anchor["text"],
                    "start_time": est_time,
                    "end_time": est_time + 3.0,
                    "estimated": True,
                })

    # Upload audio
    audio_key = f"{topic_id}/podcast_audio.mp3"
    upload_bytes_to_r2(audio_key, combined_audio, content_type="audio/mpeg")

    # Upload timestamps
    timestamp_key = f"{topic_id}/lecture_timestamps.json"
    upload_text_to_r2(timestamp_key, json.dumps({
        "total_duration": cumulative_time,
        "anchors": anchor_timings,
        "word_count": len(all_timestamps),
    }, indent=2))

    # Update topic
    supabase_client.table("topics").update({
        "podcast_audio_url": audio_key,
    }).eq("id", topic_id).execute()

    logger.info(f"Lecture audio [{topic_id}] — done. {len(anchor_timings)} anchors, {cumulative_time:.1f}s")
    return audio_key
