"""
One-time script to generate 16 filler audio clips for podcast Q&A.
Run with: python -m scripts.generate_fillers

Generates clips using Gemini multi-speaker TTS (Kore + Puck, temperature 2)
and uploads them to R2 at filler_audio/category_{a-d}_{1-4}.wav
"""

import asyncio
import base64
import io
import wave
import httpx
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services.r2 import upload_bytes_to_r2

GEMINI_TTS_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent"

SPEAKER_A = "HOST A"
SPEAKER_B = "HOST B"

FILLERS = {
    "a": [  # Reacting to something they were discussing
        f"{SPEAKER_A}: Oh — yeah yeah yeah, so that's actually the thing, right?",
        f"{SPEAKER_B}: OK so you're picking up on exactly what I was about to get into—",
        f"{SPEAKER_A}: Ha! I was literally just going to say something about that—",
        f"{SPEAKER_B}: Oh that's — yeah, that's the key question actually.",
    ],
    "b": [  # New angle
        f"{SPEAKER_A}: Ooh. OK. That's interesting, let me think about that for a second...",
        f"{SPEAKER_B}: Hmm — you know what, that's a really good question actually...",
        f"{SPEAKER_A}: Oh wow, OK — so that's a different angle but it connects...",
        f"{SPEAKER_B}: That's — yeah. OK so here's the thing about that...",
    ],
    "c": [  # Clarification
        f"{SPEAKER_A}: Right right right — OK so let me back up for a sec...",
        f"{SPEAKER_B}: Oh — yeah, I probably should have been clearer about that...",
        f"{SPEAKER_A}: OK fair, let me put it differently—",
        f"{SPEAKER_B}: Yeah so — the way I think about it is...",
    ],
    "d": [  # Pushback
        f"{SPEAKER_A}: Oh — OK I see where you're going with that...",
        f"{SPEAKER_B}: Hm. That's fair actually. So here's the thing though—",
        f"{SPEAKER_A}: Yeah, no, that's a legitimate question...",
        f"{SPEAKER_B}: Interesting — so you're saying like, why would that be the case?",
    ],
}


def pcm_to_wav(pcm_data: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_data)
    return buf.getvalue()


async def generate_filler(text: str) -> bytes:
    prompt = f"TTS the following conversation between {SPEAKER_A} and {SPEAKER_B}:\n\n{text}"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "multiSpeakerVoiceConfig": {
                    "speakerVoiceConfigs": [
                        {
                            "speaker": SPEAKER_A,
                            "voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": "Kore"}
                            }
                        },
                        {
                            "speaker": SPEAKER_B,
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

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, json=payload)

    if response.status_code != 200:
        print(f"  ERROR: {response.status_code} {response.text[:200]}")
        return None

    result = response.json()
    audio_b64 = result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    pcm_data = base64.b64decode(audio_b64)
    return pcm_to_wav(pcm_data)


async def main():
    print("Generating 16 filler audio clips...")
    print()

    for category, lines in FILLERS.items():
        for i, line in enumerate(lines, 1):
            key = f"filler_audio/category_{category}_{i}.wav"
            print(f"  Generating {key}: {line[:60]}...")

            wav_data = await generate_filler(line)
            if wav_data:
                upload_bytes_to_r2(key, wav_data, content_type="audio/wav")
                duration = (len(wav_data) - 44) / (24000 * 2)
                print(f"    ✓ {len(wav_data)} bytes, {duration:.1f}s")
            else:
                print(f"    ✗ Failed")

    print()
    print("Done! All fillers uploaded to R2 at filler_audio/")


if __name__ == "__main__":
    asyncio.run(main())
