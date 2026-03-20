"""
TTS service using Gemini 2.5 Flash TTS.
Handles both podcast (multi-speaker) and narration (single-speaker) audio generation.
"""

import io
import wave
import struct
import logging
import httpx
import base64
from app.config import settings

logger = logging.getLogger(__name__)

GEMINI_TTS_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent"
SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # 16-bit PCM


def pcm_to_wav(pcm_data: bytes) -> bytes:
    """Convert raw PCM data (16-bit, 24kHz, mono) to WAV format."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMPLE_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_data)
    return buf.getvalue()


async def generate_multi_speaker_audio(script_text: str, speaker_a_name: str = "Host", speaker_b_name: str = "Expert") -> bytes:
    """
    Generate multi-speaker audio from a podcast script using Gemini TTS.

    The script should have lines like:
        HOST: Hello and welcome...
        EXPERT: Thanks for having me...

    Returns WAV audio bytes.
    """
    # Format the prompt for Gemini multi-speaker TTS
    prompt = f"TTS the following conversation between {speaker_a_name} and {speaker_b_name}:\n\n{script_text}"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {
                            "speaker": speaker_a_name,
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": "Kore"}
                            }
                        },
                        {
                            "speaker": speaker_b_name,
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": "Puck"}
                            }
                        }
                    ]
                }
            },
            "temperature": 2.0,
        }
    }

    url = f"{GEMINI_TTS_URL}?key={settings.GOOGLE_CLOUD_API_KEY}"

    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(url, json=payload)

    if response.status_code != 200:
        logger.error(f"Gemini TTS error: {response.status_code} {response.text[:500]}")
        raise ValueError(f"Gemini TTS failed: {response.status_code}")

    result = response.json()

    # Extract base64 audio data
    audio_b64 = result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    pcm_data = base64.b64decode(audio_b64)

    # Convert PCM to WAV
    wav_data = pcm_to_wav(pcm_data)
    logger.info(f"Multi-speaker TTS — generated {len(wav_data)} bytes WAV audio")

    return wav_data


async def generate_single_speaker_audio(text: str, voice_name: str = "Kore") -> bytes:
    """
    Generate single-speaker audio using Gemini TTS.
    Used for visual overview narration.

    Returns WAV audio bytes.
    """
    payload = {
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice_name}
                }
            },
        }
    }

    url = f"{GEMINI_TTS_URL}?key={settings.GOOGLE_CLOUD_API_KEY}"

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(url, json=payload)

    if response.status_code != 200:
        logger.error(f"Gemini TTS error: {response.status_code} {response.text[:500]}")
        raise ValueError(f"Gemini TTS failed: {response.status_code}")

    result = response.json()

    audio_b64 = result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    pcm_data = base64.b64decode(audio_b64)

    wav_data = pcm_to_wav(pcm_data)
    logger.info(f"Single-speaker TTS — generated {len(wav_data)} bytes WAV audio")

    return wav_data
