"""
Travel advisor voice interaction.
Loads YAML destination cards from R2, streams Claude + Inworld TTS.
Same streaming pipe as podcast ask-stream, different system prompt and data source.
"""

import json
import base64
import logging
import httpx
import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from sse_starlette.sse import EventSourceResponse
from starlette.responses import StreamingResponse
from app.config import settings
from app.middleware.clerk_auth import get_current_student
from app.services.r2 import download_from_r2

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/travel", tags=["travel"])

# ── YAML card keys on R2 ──
DESTINATION_CARD_KEYS = [
    "travel/jamaica-destination-card.yaml",
    "travel/antigua-barbuda-destination-card.yaml",
    "travel/trinidad-tobago-destination-card.yaml",
    "travel/barbados-destination-card.yaml",
]


def _load_destination_cards() -> str:
    """Load all YAML destination cards from R2, return combined text."""
    cards = []
    for key in DESTINATION_CARD_KEYS:
        try:
            raw = download_from_r2(key).decode("utf-8")
            cards.append(raw)
        except Exception as e:
            logger.warning(f"Could not load {key}: {e}")
    return "\n\n---\n\n".join(cards)


TRAVEL_SYSTEM_PROMPT = """You are Sam, a destination intelligence assistant for travel advisors. Fast, accurate, conversational.

You are talking to a travel advisor, not a client. They need answers they can use on a call.

Your knowledge comes from structured destination intelligence cards. If something isn't covered, say so.

RESPONSE FORMAT — THIS IS NON-NEGOTIABLE:
- MAX 3 sentences per response. Count them. If you wrote more than 3, delete until you have 3.
- Give ONE recommendation. Not two. Not three. ONE. The best fit. If they want alternatives, they'll ask.
- NEVER use bold text, asterisks, bullet points, dashes, lists, or headers. Plain speech only.
- This is SPOKEN AUDIO. The advisor is listening, not reading. Talk like a colleague in the hallway.

EXAMPLES OF GOOD RESPONSES:
When advisor hasn't given enough detail: "Are they after romantic and secluded or more of a social energy? And do they care about loyalty points — Marriott, Hyatt, anything like that?"
When advisor has given enough detail: "Sandals Grande on Dickenson Bay — voted most romantic resort 14 years running, Rondoval suites with plunge pools, and it's right on the best beach in Antigua."

WHAT YOU DO:
- ASK FIRST. When the advisor gives you a scenario, ask the one or two things you need to give a precise recommendation. Don't guess and then ask if you guessed right.
- Example: advisor says "client wants adults-only in Antigua." You say: "Are they after romantic and secluded, or do they want energy and nightlife? And what's the budget range?" Then when they answer, you give the ONE perfect fit.
- Once you have enough to recommend, lead with the answer. Name, location, why it fits. One breath.
- If the advisor already gave you everything you need, skip the questions and recommend.
- If a property is closed, say so and give the alternative in the same sentence.
- Flag safety or advisory issues naturally.
- Be honest about what you don't know.
- Never say "YAML", "destination card", "data source", or anything technical.

DESTINATION INTELLIGENCE:

"""


# ── Sentence chunking (same as podcast ask-stream) ──

def _has_tts_chunk(buffer: str) -> bool:
    import re
    if re.search(r'[.!?]\s', buffer):
        return True
    return False


def _extract_tts_chunk(buffer: str) -> tuple:
    import re
    sentence_match = re.search(r'([.!?])\s', buffer)
    if sentence_match:
        end = sentence_match.end()
        return buffer[:end].strip(), buffer[end:]
    return buffer, ""


# ── Streaming endpoint ──

@router.post("/ask-stream")
async def travel_ask_stream(request: Request, student: dict = Depends(get_current_student)):
    """Travel advisor with true streaming — Claude streams, Inworld TTS fires at sentence boundaries."""
    body = await request.json()
    audio_b64_input = body.get("audio")
    text_question = body.get("text")
    history = body.get("history", [])

    # Get question text (voice or typed)
    question = text_question
    if audio_b64_input and not question:
        audio_bytes = base64.b64decode(audio_b64_input)
        async with httpx.AsyncClient() as client:
            stt_response = await client.post(
                "https://api.deepgram.com/v1/listen",
                params={"model": "nova-3", "smart_format": "true", "language": "en"},
                headers={
                    "Authorization": f"Token {settings.DEEPGRAM_API_KEY}",
                    "Content-Type": "audio/webm",
                },
                content=audio_bytes,
                timeout=30.0,
            )
        try:
            question = stt_response.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
        except (KeyError, IndexError):
            raise HTTPException(502, "Transcription failed")

    if not question or not question.strip():
        return {"transcript": "", "answer": "", "audio": None}

    # Load destination cards
    cards_text = _load_destination_cards()
    system_prompt = TRAVEL_SYSTEM_PROMPT + cards_text

    # Messages with history
    api_messages = []
    for msg in history:
        if msg.get("role") and msg.get("content"):
            api_messages.append({"role": msg["role"], "content": msg["content"]})
    api_messages.append({"role": "user", "content": question})

    # The streaming generator
    async def generate_stream():
        yield f"data: {json.dumps({'type': 'transcript', 'text': question})}\n\n"
        yield f"data: {json.dumps({'type': 'thinking'})}\n\n"

        client = anthropic.AsyncAnthropic()
        full_response = ""
        sentence_buffer = ""
        chunk_index = 0
        tts_client = httpx.AsyncClient(timeout=30.0)

        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6-20250220",
                max_tokens=2048,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"}
                }],
                messages=api_messages,
            ) as stream:
                async for text in stream.text_stream:
                    full_response += text
                    sentence_buffer += text

                    while _has_tts_chunk(sentence_buffer):
                        sentence, sentence_buffer = _extract_tts_chunk(sentence_buffer)

                        if not sentence.strip():
                            continue

                        yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence})}\n\n"

                        # TTS via Inworld
                        try:
                            tts_response = await tts_client.post(
                                "https://api.inworld.ai/tts/v1/voice",
                                headers={
                                    "Authorization": f"Basic {settings.INWORLD_API_KEY}",
                                    "Content-Type": "application/json",
                                },
                                json={
                                    "text": sentence.strip(),
                                    "voice_id": "Dennis",
                                    "model_id": "inworld-tts-1.5-max",
                                    "audio_config": {
                                        "audio_encoding": "MP3",
                                    },
                                },
                            )

                            if tts_response.status_code == 200:
                                tts_json = tts_response.json()
                                audio_b64 = tts_json.get("audioContent") or tts_json.get("result", {}).get("audioContent")
                                if audio_b64:
                                    yield f"data: {json.dumps({'type': 'audio_chunk', 'index': chunk_index, 'audio': audio_b64, 'format': 'mp3'})}\n\n"
                                else:
                                    logger.warning(f"TTS chunk {chunk_index} — no audioContent")
                            else:
                                logger.warning(f"TTS chunk {chunk_index} failed: {tts_response.status_code}")
                                yield f"data: {json.dumps({'type': 'tts_error', 'index': chunk_index, 'error': f'HTTP {tts_response.status_code}'})}\n\n"

                        except Exception as e:
                            logger.error(f"TTS chunk {chunk_index} failed: {e}")
                            yield f"data: {json.dumps({'type': 'tts_error', 'index': chunk_index, 'error': str(e)})}\n\n"

                        chunk_index += 1

            # Handle remaining buffer
            if sentence_buffer.strip():
                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence_buffer})}\n\n"

                try:
                    tts_response = await tts_client.post(
                        "https://api.inworld.ai/tts/v1/voice",
                        headers={
                            "Authorization": f"Basic {settings.INWORLD_API_KEY}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "text": sentence_buffer.strip(),
                            "voice_id": "Dennis",
                            "model_id": "inworld-tts-1.5-max",
                            "audio_config": {
                                "audio_encoding": "MP3",
                            },
                        },
                    )

                    if tts_response.status_code == 200:
                        tts_json = tts_response.json()
                        audio_b64 = tts_json.get("audioContent") or tts_json.get("result", {}).get("audioContent")
                        if audio_b64:
                            yield f"data: {json.dumps({'type': 'audio_chunk', 'index': chunk_index, 'audio': audio_b64, 'format': 'mp3'})}\n\n"
                except Exception as e:
                    logger.error(f"TTS final chunk failed: {e}")

        except Exception as e:
            logger.error(f"Travel streaming failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            await tts_client.aclose()

        yield f"data: {json.dumps({'type': 'answer', 'text': full_response})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
