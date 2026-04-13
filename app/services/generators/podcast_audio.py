"""
Lecture audio generator.
Uses Inworld TTS (voice: Kelsey) with word-level timestamps.
Supports per-segment generation via lecture manifest, with full-script fallback.
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


def _clean_script_for_tts(script: str) -> str:
    """Remove markers and speaker labels so TTS gets clean text."""
    clean = re.sub(r'\[ANCHOR:\s*"[^"]+"\]', '', script)
    clean = re.sub(r'\[IMAGE_PROMPT:\s*"[^"]+"\]', '', clean)
    clean = re.sub(r'\[PAUSE\]', '', clean)
    clean = re.sub(r'^(TEACHER|STUDENT_CHLOE|STUDENT_NATE|STUDENT_MIA):\s*', '', clean, flags=re.MULTILINE)
    clean = re.sub(r'\n{3,}', '\n\n', clean)
    return clean.strip()


def _chunk_text(text: str, max_chars: int = 2000) -> list[str]:
    """Split text at paragraph boundaries into chunks under max_chars."""
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""
    for para in paragraphs:
        if len(current) + len(para) + 2 > max_chars and current:
            chunks.append(current.strip())
            current = ""
        current += para + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


async def _tts_chunks(topic_id: str, label: str, chunks: list[str]) -> tuple:
    """
    TTS a list of text chunks with retry. Returns (audio_parts, timestamps, total_duration).
    audio_parts: list of bytes (MP3 segments)
    timestamps: list of word timestamp dicts
    total_duration: float seconds
    """
    all_audio_parts = []
    all_timestamps = []
    cumulative_time = 0.0

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(1)
        logger.info(f"Lecture audio [{topic_id}] — {label} TTS chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                result = await inworld_tts(chunk, voice_id="Kelsey", get_timestamps=True)
                break
            except Exception as e:
                if attempt < max_retries:
                    logger.warning(f"Lecture audio [{topic_id}] — {label} chunk {i+1} failed (attempt {attempt+1}), retrying: {e}")
                    await asyncio.sleep(2)
                else:
                    raise

        if not result or not result.get("audio"):
            logger.error(f"Lecture audio [{topic_id}] — {label} chunk {i+1} no audio")
            continue

        all_audio_parts.append(base64.b64decode(result["audio"]))

        if result.get("timestamps"):
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

    return all_audio_parts, all_timestamps, cumulative_time


def _match_anchors_to_timestamps(anchors: list[dict], all_timestamps: list[dict], cumulative_time: float) -> list[dict]:
    """Match anchor text to word timestamps. Falls back to char-position estimation."""
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
    return anchor_timings


async def generate_podcast_audio(topic_id: str, supabase_client) -> str:
    """Generate lecture audio. Uses per-segment if manifest exists, otherwise full-script."""

    # Try to load segment manifest
    manifest = None
    try:
        manifest_bytes = download_from_r2(f"{topic_id}/lecture/manifest.json")
        manifest = json.loads(manifest_bytes.decode("utf-8"))
        logger.info(f"Lecture audio [{topic_id}] — found manifest with {manifest['segment_count']} segments")
    except Exception:
        logger.info(f"Lecture audio [{topic_id}] — no manifest, using full-script mode")

    if manifest:
        return await _generate_per_segment(topic_id, supabase_client, manifest)
    else:
        return await _generate_full_script(topic_id, supabase_client)


async def _generate_per_segment(topic_id: str, supabase_client, manifest: dict) -> str:
    """Generate audio for each segment in the lecture manifest."""

    for seg in manifest["segments"]:
        seg_num = seg["number"]

        # Load segment script
        script_bytes = download_from_r2(seg["script_url"])
        script = script_bytes.decode("utf-8")

        # Extract anchors before cleaning
        anchors = []
        for match in re.finditer(r'\[ANCHOR:\s*"([^"]+)"\]', script):
            anchors.append({"text": match.group(1), "char_position": match.start()})

        # Clean for TTS
        clean_script = _clean_script_for_tts(script)

        if not clean_script:
            logger.warning(f"Lecture audio [{topic_id}] — segment {seg_num} is empty after cleaning, skipping")
            continue

        logger.info(f"Lecture audio [{topic_id}] — generating audio for segment {seg_num} ({len(clean_script)} chars)")

        # Chunk and TTS
        chunks = _chunk_text(clean_script)
        audio_parts, timestamps, duration = await _tts_chunks(topic_id, f"seg{seg_num}", chunks)

        if not audio_parts:
            logger.error(f"Lecture audio [{topic_id}] — no audio generated for segment {seg_num}")
            continue

        combined_audio = b"".join(audio_parts)

        # Store audio
        audio_key = f"{topic_id}/lecture/segment_{seg_num}.mp3"
        upload_bytes_to_r2(audio_key, combined_audio, content_type="audio/mpeg")
        seg["audio_url"] = audio_key

        # Match anchors and store timestamps
        anchor_timings = _match_anchors_to_timestamps(anchors, timestamps, duration)
        timestamps_data = {
            "duration": duration,
            "anchors": anchor_timings,
            "word_count": len(timestamps),
        }
        ts_key = f"{topic_id}/lecture/segment_{seg_num}_timestamps.json"
        upload_text_to_r2(ts_key, json.dumps(timestamps_data, indent=2))
        seg["timestamps_url"] = ts_key

        logger.info(f"Lecture audio [{topic_id}] — segment {seg_num} complete ({len(combined_audio)} bytes, {duration:.1f}s)")

    # Update manifest with audio URLs
    manifest_key = f"{topic_id}/lecture/manifest.json"
    upload_text_to_r2(manifest_key, json.dumps(manifest, indent=2))

    # Update topic for backward compat
    supabase_client.table("topics").update({
        "podcast_audio_url": manifest_key
    }).eq("id", topic_id).execute()

    logger.info(f"Lecture audio [{topic_id}] — all segments complete")
    return manifest_key


async def _generate_full_script(topic_id: str, supabase_client) -> str:
    """Legacy: generate one audio file from the full script (no manifest)."""

    topic = supabase_client.table("topics").select("podcast_script_url").eq("id", topic_id).execute()
    if not topic.data or not topic.data[0].get("podcast_script_url"):
        raise Exception("No lecture script found")

    script_bytes = download_from_r2(topic.data[0]["podcast_script_url"])
    script = script_bytes.decode("utf-8")
    logger.info(f"Lecture audio [{topic_id}] — loaded full script ({len(script)} chars)")

    # Extract anchors
    anchors = []
    for match in re.finditer(r'\[ANCHOR:\s*"([^"]+)"\]', script):
        anchors.append({"text": match.group(1), "char_position": match.start()})

    # Clean and chunk
    clean_script = _clean_script_for_tts(script)
    chunks = _chunk_text(clean_script)

    logger.info(f"Lecture audio [{topic_id}] — {len(chunks)} chunks, {len(anchors)} anchors")

    audio_parts, timestamps, duration = await _tts_chunks(topic_id, "full", chunks)

    if not audio_parts:
        raise Exception("No audio generated")

    combined_audio = b"".join(audio_parts)
    anchor_timings = _match_anchors_to_timestamps(anchors, timestamps, duration)

    # Upload audio
    audio_key = f"{topic_id}/podcast_audio.mp3"
    upload_bytes_to_r2(audio_key, combined_audio, content_type="audio/mpeg")

    # Upload timestamps
    timestamp_key = f"{topic_id}/lecture_timestamps.json"
    upload_text_to_r2(timestamp_key, json.dumps({
        "total_duration": duration,
        "anchors": anchor_timings,
        "word_count": len(timestamps),
    }, indent=2))

    supabase_client.table("topics").update({
        "podcast_audio_url": audio_key,
    }).eq("id", topic_id).execute()

    logger.info(f"Lecture audio [{topic_id}] — done. {len(anchor_timings)} anchors, {duration:.1f}s")
    return audio_key
