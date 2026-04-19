"""
Lecture audio generator.
Uses Gemini multi-speaker TTS via the AI Studio generativelanguage endpoint (2 voices):
  EXPERT (Aoede voice), HOST (Charon voice).
Speaker labels in the script are PRESERVED so Gemini can route each line.
Returns LINEAR16 PCM, wrapped in WAV header. Stores WAV + timestamp JSON on R2.
"""

import asyncio
import base64
import json
import re
import struct
import logging
import httpx
from app.config import settings
from app.services.r2 import download_from_r2, upload_text_to_r2, upload_bytes_to_r2

logger = logging.getLogger(__name__)

GEMINI_TTS_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent"
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM

# Gemini multi-speaker voice mapping (hard limit: 2 speakers)
SPEAKER_VOICE_CONFIGS = [
    {"speaker": "EXPERT", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Aoede"}}},
    {"speaker": "HOST", "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": "Charon"}}},
]


def _pcm_to_wav(pcm_data: bytes) -> bytes:
    """Wrap raw PCM data (16-bit, 24kHz, mono) in a WAV header."""
    data_size = len(pcm_data)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, CHANNELS, SAMPLE_RATE,
        SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH,
        CHANNELS * SAMPLE_WIDTH, SAMPLE_WIDTH * 8,
        b'data', data_size,
    )
    return header + pcm_data


# Map any speaker label to EXPERT or HOST for the 2-voice TTS
_SPEAKER_ALIAS = {
    "EXPERT": "EXPERT",
    "HOST": "HOST",
    "AEODE": "EXPERT",
    "CHARON": "HOST",
    "TEACHER": "EXPERT",
    "KORE": "HOST",
    "ZEPHYR": "HOST",
    "STUDENT": "HOST",
    "STUDENT_CHLOE": "HOST",
    "STUDENT_NATE": "HOST",
    "STUDENT_MIA": "HOST",
}

_ALL_LABELS = "|".join(_SPEAKER_ALIAS.keys())
_SPEAKER_RE = re.compile(rf'^({_ALL_LABELS}):', re.MULTILINE)


def _clean_script_for_gemini(script: str) -> str:
    """
    Clean script for Gemini multi-speaker TTS.
    KEEP speaker labels (EXPERT/HOST) — Gemini uses them to assign voices.
    Remove markers only.
    """
    clean = re.sub(r'\[ANCHOR:\s*"[^"]+"\]', '', script)
    clean = re.sub(r'\[IMAGE_PROMPT:\s*"[^"]+"\]', '', clean)
    clean = re.sub(r'\[PAUSE\]', '...', clean)  # natural pause
    clean = re.sub(r'\n{3,}', '\n\n', clean)
    return clean.strip()


def _chunk_by_speaker(text: str, max_chars: int = 4000) -> list[str]:
    """
    Split text at speaker-line boundaries into chunks under max_chars.
    Never breaks a speaker turn across chunks.
    """
    # Split into speaker turns — matches any known speaker label
    speaker_pattern = _SPEAKER_RE
    starts = [m.start() for m in speaker_pattern.finditer(text)]

    if not starts:
        # No speaker labels found — fall back to paragraph chunking
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

    # Build turn list: each turn is text from its start to the next speaker's start
    turns = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        turns.append(text[start:end].strip())

    # Pack turns into chunks respecting max_chars
    chunks = []
    current = ""
    for turn in turns:
        if len(current) + len(turn) + 2 > max_chars and current:
            chunks.append(current.strip())
            current = ""
        current += turn + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _parse_speaker_turns(script_text: str) -> list[dict]:
    """
    Parse script text with speaker labels into structured turns.
    Used here only as a validity check (AI Studio endpoint takes the raw
    labeled text directly), but retained so callers can detect empty chunks.
    Handles EXPERT/HOST, AEODE/CHARON, TEACHER, and student labels.
    All labels are mapped to EXPERT or HOST (2-voice limit).
    """
    turns = []
    starts = [m.start() for m in _SPEAKER_RE.finditer(script_text)]

    if not starts:
        logger.error(f"No speaker labels found. First 300 chars: {script_text[:300]}")
        return turns

    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(script_text)
        block = script_text[start:end].strip()

        match = re.match(rf'^({_ALL_LABELS}):\s*(.*)', block, re.DOTALL)
        if match:
            raw_speaker = match.group(1)
            text = match.group(2).strip()
            speaker = _SPEAKER_ALIAS.get(raw_speaker, "EXPERT")
            if text:
                turns.append({"speaker": speaker, "text": text})

    return turns


async def _gemini_multi_speaker_tts(chunk_text: str, tts_client: httpx.AsyncClient) -> bytes:
    """
    Call Gemini multi-speaker TTS (AI Studio endpoint). Returns raw PCM bytes.
    Sanity-check speaker labels first (skip if none) so we don't waste a request.
    """
    if not _parse_speaker_turns(chunk_text):
        logger.warning(f"No speaker turns in chunk, skipping. First 200 chars: {chunk_text[:200]}")
        return b""  # Return empty audio — caller will skip

    prompt = f"TTS the following dialogue between EXPERT and HOST:\n\n{chunk_text}"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": SPEAKER_VOICE_CONFIGS,
                }
            },
            "temperature": 2.0,
        },
    }

    url = f"{GEMINI_TTS_URL}?key={settings.GOOGLE_CLOUD_API_KEY}"
    response = await tts_client.post(url, json=payload)

    if response.status_code != 200:
        logger.error(f"Gemini TTS error — HTTP {response.status_code} — body: {response.text[:500]}")
        raise Exception(f"Gemini TTS failed: HTTP {response.status_code} — {response.text[:200]}")

    data = response.json()
    try:
        audio_b64 = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    except (KeyError, IndexError) as e:
        raise Exception(f"Gemini TTS response malformed: {e} — body: {str(data)[:300]}")

    return base64.b64decode(audio_b64)


async def _tts_chunks(topic_id: str, label: str, chunks: list[str]) -> tuple:
    """
    TTS a list of chunks via Gemini multi-speaker, with retry + rate limiting.
    Returns (combined_pcm_bytes, total_duration_seconds).
    """
    all_pcm_parts = []

    async with httpx.AsyncClient(timeout=300.0) as tts_client:
        for i, chunk in enumerate(chunks):
            if i > 0:
                await asyncio.sleep(2)  # rate limit spacing
            logger.info(f"Lecture audio [{topic_id}] — {label} TTS chunk {i+1}/{len(chunks)} ({len(chunk)} chars)")

            max_retries = 2
            pcm = None
            for attempt in range(max_retries + 1):
                try:
                    pcm = await _gemini_multi_speaker_tts(chunk, tts_client)
                    break
                except Exception as e:
                    if attempt < max_retries:
                        logger.warning(f"Lecture audio [{topic_id}] — {label} chunk {i+1} failed (attempt {attempt+1}), retrying: {e}")
                        await asyncio.sleep(3)
                    else:
                        raise

            if not pcm:
                logger.error(f"Lecture audio [{topic_id}] — {label} chunk {i+1} no audio")
                continue

            all_pcm_parts.append(pcm)

    combined_pcm = b"".join(all_pcm_parts)
    # Duration from PCM size: bytes / (sample_rate * sample_width * channels)
    duration = len(combined_pcm) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
    return combined_pcm, duration


def _estimate_anchor_timings(anchors: list[dict], total_duration: float, script_length: int) -> list[dict]:
    """
    Estimate anchor timings from character position in the script.
    Gemini doesn't return word timestamps, so we use proportional placement.
    """
    anchor_timings = []
    if script_length <= 0 or total_duration <= 0:
        return anchor_timings

    for anchor in anchors:
        ratio = anchor["char_position"] / script_length
        est_time = ratio * total_duration
        anchor_timings.append({
            "text": anchor["text"],
            "start_time": est_time,
            "end_time": est_time + 3.0,
            "estimated": True,
        })
    return anchor_timings


async def generate_podcast_audio(topic_id: str, supabase_client) -> str:
    """Generate lecture audio. Uses per-segment if manifest exists, otherwise full-script."""

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
    """Generate audio for each segment in the lecture manifest via Gemini multi-speaker."""

    for seg in manifest["segments"]:
        seg_num = seg["number"]

        script_bytes = download_from_r2(seg["script_url"])
        script = script_bytes.decode("utf-8")

        # Extract anchors before cleaning
        anchors = []
        for match in re.finditer(r'\[ANCHOR:\s*"([^"]+)"\]', script):
            anchors.append({"text": match.group(1), "char_position": match.start()})

        clean_script = _clean_script_for_gemini(script)

        if not clean_script:
            logger.warning(f"Lecture audio [{topic_id}] — segment {seg_num} is empty after cleaning, skipping")
            continue

        logger.info(f"Lecture audio [{topic_id}] — generating audio for segment {seg_num} ({len(clean_script)} chars)")

        chunks = _chunk_by_speaker(clean_script)
        combined_pcm, duration = await _tts_chunks(topic_id, f"seg{seg_num}", chunks)

        if not combined_pcm:
            logger.error(f"Lecture audio [{topic_id}] — no audio generated for segment {seg_num}")
            continue

        # Wrap combined PCM in WAV header once
        wav_data = _pcm_to_wav(combined_pcm)

        # Store audio
        audio_key = f"{topic_id}/lecture/segment_{seg_num}.wav"
        upload_bytes_to_r2(audio_key, wav_data, content_type="audio/wav")
        seg["audio_url"] = audio_key

        # Estimate anchor timings from char position
        anchor_timings = _estimate_anchor_timings(anchors, duration, len(clean_script))
        timestamps_data = {
            "duration": duration,
            "anchors": anchor_timings,
            "word_count": 0,  # Gemini doesn't provide word timestamps
        }
        ts_key = f"{topic_id}/lecture/segment_{seg_num}_timestamps.json"
        upload_text_to_r2(ts_key, json.dumps(timestamps_data, indent=2))
        seg["timestamps_url"] = ts_key

        logger.info(f"Lecture audio [{topic_id}] — segment {seg_num} complete ({len(wav_data)} bytes, {duration:.1f}s)")

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

    anchors = []
    for match in re.finditer(r'\[ANCHOR:\s*"([^"]+)"\]', script):
        anchors.append({"text": match.group(1), "char_position": match.start()})

    clean_script = _clean_script_for_gemini(script)
    chunks = _chunk_by_speaker(clean_script)

    logger.info(f"Lecture audio [{topic_id}] — {len(chunks)} chunks, {len(anchors)} anchors")

    combined_pcm, duration = await _tts_chunks(topic_id, "full", chunks)

    if not combined_pcm:
        raise Exception("No audio generated")

    wav_data = _pcm_to_wav(combined_pcm)
    anchor_timings = _estimate_anchor_timings(anchors, duration, len(clean_script))

    audio_key = f"{topic_id}/podcast_audio.wav"
    upload_bytes_to_r2(audio_key, wav_data, content_type="audio/wav")

    timestamp_key = f"{topic_id}/lecture_timestamps.json"
    upload_text_to_r2(timestamp_key, json.dumps({
        "total_duration": duration,
        "anchors": anchor_timings,
        "word_count": 0,
    }, indent=2))

    supabase_client.table("topics").update({
        "podcast_audio_url": audio_key,
    }).eq("id", topic_id).execute()

    logger.info(f"Lecture audio [{topic_id}] — done. {len(anchor_timings)} anchors, {duration:.1f}s")
    return audio_key
