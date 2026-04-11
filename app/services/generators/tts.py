"""
TTS service — Inworld TTS (Kelsey voice, MP3 output).
All student-facing audio uses this single function.
"""

import logging
import httpx
from app.config import settings

logger = logging.getLogger(__name__)


async def inworld_tts(text: str, voice_id: str = "Kelsey", get_timestamps: bool = False) -> dict:
    """Generate audio via Inworld TTS. Returns {"audio": base64_string, "timestamps": {...} or None}"""
    payload = {
        "text": text,
        "voice_id": voice_id,
        "model_id": "inworld-tts-1.5-max",
        "audio_config": {"audio_encoding": "MP3"},
    }
    if get_timestamps:
        payload["timestampType"] = "WORD"

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            "https://api.inworld.ai/tts/v1/voice",
            headers={
                "Authorization": f"Basic {settings.INWORLD_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )

    if response.status_code != 200:
        logger.error(f"Inworld TTS error — HTTP {response.status_code} — body: {response.text[:500]}")
        raise Exception(f"Inworld TTS failed: HTTP {response.status_code} — {response.text[:200]}")

    data = response.json()
    result = {
        "audio": data.get("audioContent") or data.get("result", {}).get("audioContent"),
        "timestamps": None,
    }
    ts_info = data.get("timestampInfo") or data.get("result", {}).get("timestampInfo")
    if ts_info and "wordAlignment" in ts_info:
        result["timestamps"] = ts_info["wordAlignment"]
    return result
