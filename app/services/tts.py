"""
ElevenLabs TTS service with per-language voice routing.
"""

import base64
import json
import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

VOICE_MAP = {
    "en": {"voice_id": "Cz0K1kOv9tD8l0b5Qu53", "model_id": "eleven_multilingual_v2"},
    "hi": {"voice_id": "Uyx98Ek4uMNmWN7E28CD", "model_id": "eleven_multilingual_v2"},
    "pa": {"voice_id": "or7B7ER8jJv0zLEHTMQu", "model_id": "eleven_v3"},
    "ur": {"voice_id": "k7nOSUCadIEwB6fdJmbw", "model_id": "eleven_v3"},
    "gu": {"voice_id": "bpkHhw4QrZFYIfahwsHh", "model_id": "eleven_v3"},
    "ta": {"voice_id": "9Ats6C5UrhVXzgyVbnh3", "model_id": "eleven_multilingual_v2"},
    "te": {"voice_id": "9Ats6C5UrhVXzgyVbnh3", "model_id": "eleven_v3"},
    "bn": {"voice_id": "WiaIVvI1gDL4vT4y7qUU", "model_id": "eleven_v3"},
    "ml": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "mr": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "ne": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "zh": {"voice_id": "hkfHEbBvdQFNX4uWHqRF", "model_id": "eleven_multilingual_v2"},
    "ko": {"voice_id": "1W00IGEmNmwmsDeYy7ag", "model_id": "eleven_multilingual_v2"},
    "ja": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_multilingual_v2"},
    "vi": {"voice_id": "FTYCiQT21H9XQvhRu0ch", "model_id": "eleven_v3"},
    "th": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "tl": {"voice_id": "226pMx2LkIXBm6nGVIzc", "model_id": "eleven_multilingual_v2"},
    "id": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_multilingual_v2"},
    "ar": {"voice_id": "wxweiHvoC2r2jFM7mS8b", "model_id": "eleven_multilingual_v2"},
    "tr": {"voice_id": "Md4RAnfKt9kVIbvqUxly", "model_id": "eleven_multilingual_v2"},
    "fa": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "he": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "sw": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "so": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "ha": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_v3"},
    "uk": {"voice_id": "TEyBWD5tAHAWqAGEv6yI", "model_id": "eleven_multilingual_v2"},
    "ru": {"voice_id": "HcaxAsrhw4ByUo4CBCBN", "model_id": "eleven_multilingual_v2"},
    "ro": {"voice_id": "HPdbgrGubKiBta6Pq21b", "model_id": "eleven_multilingual_v2"},
    "pl": {"voice_id": "H5xTcsAIeS5RAykjz57a", "model_id": "eleven_multilingual_v2"},
    "hr": {"voice_id": "DAGnQ7r9sMtV0Q44g1Di", "model_id": "eleven_multilingual_v2"},
    "sr": {"voice_id": "DAGnQ7r9sMtV0Q44g1Di", "model_id": "eleven_v3"},
    "bs": {"voice_id": "DAGnQ7r9sMtV0Q44g1Di", "model_id": "eleven_v3"},
    "fr": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_multilingual_v2"},
    "es": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_multilingual_v2"},
    "pt-br": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_multilingual_v2"},
    "pt": {"voice_id": "21m00Tcm4TlvDq8ikWAM", "model_id": "eleven_multilingual_v2"},
}

DEFAULT_VOICE = {"voice_id": "Cz0K1kOv9tD8l0b5Qu53", "model_id": "eleven_multilingual_v2"}

COUNTERPART_VOICE_MAP = {
    "en": {"voice_id": "TX3LPaxmHKxFdv7VOQHJ", "model_id": "eleven_multilingual_v2"},
    "hi": {"voice_id": "N2al4jd45e882svx17SU", "model_id": "eleven_multilingual_v2"},
    "ar": {"voice_id": "IES4nrmZdUBHByLBde0P", "model_id": "eleven_multilingual_v2"},
    "tr": {"voice_id": "fIkMvhlUiPDH5oeAd0Sx", "model_id": "eleven_multilingual_v2"},
    "uk": {"voice_id": "GVRiwBELe0czFUAJj0nX", "model_id": "eleven_multilingual_v2"},
    "ru": {"voice_id": "ogi2DyUAKJb7CEdqqvlU", "model_id": "eleven_multilingual_v2"},
    "ro": {"voice_id": "S98OhkhaxeAKHEbhoLi7", "model_id": "eleven_multilingual_v2"},
    "pl": {"voice_id": "S1JKkpuAQNsowB8ZvKRO", "model_id": "eleven_multilingual_v2"},
    "hr": {"voice_id": "ZLYZToA7aDsMbHwM9AOr", "model_id": "eleven_multilingual_v2"},
}
DEFAULT_COUNTERPART = {"voice_id": "TX3LPaxmHKxFdv7VOQHJ", "model_id": "eleven_multilingual_v2"}


def get_counterpart_voice(language_code: str) -> dict:
    return COUNTERPART_VOICE_MAP.get(language_code, DEFAULT_COUNTERPART)


def get_voice_for_language(language_code: str) -> dict:
    return VOICE_MAP.get(language_code, DEFAULT_VOICE)


async def tts_chunk(
    client: httpx.AsyncClient,
    text: str,
    index: int,
    voice_id: str = None,
    model_id: str = None,
    language: str = None,
) -> str:
    if voice_id:
        voice = voice_id
        model = model_id or "eleven_flash_v2_5"
    elif language:
        config = get_voice_for_language(language)
        voice = config["voice_id"]
        model = config["model_id"]
    else:
        voice = DEFAULT_VOICE["voice_id"]
        model = DEFAULT_VOICE["model_id"]

    try:
        response = await client.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
            headers={
                "xi-api-key": settings.ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text.strip(),
                "model_id": model,
                "voice_settings": {
                    "stability": 0.45,
                    "similarity_boost": 0.75,
                    "style": 0.25,
                    "use_speaker_boost": True,
                },
            },
        )
        if response.status_code == 200:
            audio_b64 = base64.b64encode(response.content).decode("utf-8")
            return f"data: {json.dumps({'type': 'audio_chunk', 'index': index, 'audio': audio_b64, 'format': 'mp3'})}\n\n"
        return f"data: {json.dumps({'type': 'tts_error', 'index': index, 'error': f'HTTP {response.status_code}'})}\n\n"
    except Exception as e:
        logger.error(f"TTS chunk {index} failed: {e}")
        return f"data: {json.dumps({'type': 'tts_error', 'index': index, 'error': str(e)})}\n\n"
