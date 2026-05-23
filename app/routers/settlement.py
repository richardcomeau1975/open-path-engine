"""
Settlement domain routes.

Phase 2: the generator endpoint. Produces the dot-based representation.
Phase 3: the conversation endpoint. Streams a conversation anchored to that
representation, mirroring the travel ask-stream pipe (Claude stream + Inworld TTS).
"""

import asyncio
import base64
import json
import logging
import re
import uuid

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse

from app.config import settings
from app.middleware.clerk_auth import get_current_student
from app.services.settlement_generator import generate_settlement_asset
from app.services.r2 import upload_text_to_r2

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settlement", tags=["settlement"])


# ── Generator endpoint (Phase 2, unchanged) ──

@router.post("/generate")
async def settlement_generate(request: Request, student: dict = Depends(get_current_student)):
    """Run the settlement generator for a situation."""
    body = await request.json()
    situation_text = (body.get("situation_text") or "").strip()
    client_need = (body.get("client_need") or "").strip()

    if not situation_text:
        raise HTTPException(status_code=400, detail="situation_text is required")
    if not client_need:
        raise HTTPException(status_code=400, detail="client_need is required")

    try:
        asset = await asyncio.to_thread(
            generate_settlement_asset, situation_text, client_need
        )
    except json.JSONDecodeError as e:
        logger.error(f"Settlement generator returned invalid JSON: {e}")
        raise HTTPException(status_code=502, detail="Generator did not return valid JSON")
    except Exception as e:
        logger.error(f"Settlement generation failed: {e}")
        raise HTTPException(status_code=502, detail="Settlement generation failed")

    asset_id = str(uuid.uuid4())
    r2_key = f"settlement/{student['id']}/{asset_id}.json"
    try:
        upload_text_to_r2(r2_key, json.dumps(asset))
    except Exception as e:
        logger.error(f"Could not store settlement asset on R2: {e}")
        r2_key = None

    return {"asset_id": asset_id, "r2_key": r2_key, "asset": asset}


# ── Conversation prompt (Phase 3, FIRST-DRAFT slot-in — operator refines) ──

SETTLEMENT_CONVERSATION_PROMPT = """You are a settlement navigation assistant. You are helping a newcomer to Canada understand and navigate a specific real situation they are facing. You are speaking with the person themselves.

Below the line you are given the structured representation of their situation: a set of dots. Each dot is a capability the person needs in order to navigate this situation. Each dot carries a fluency dimension, what doing it haltingly looks like and what doing it fluently looks like. The dots are grouped into clusters joined by a chain.

THE DOTS GOVERN THIS CONVERSATION. Do not freelance on the situation. Everything you say should be building one of the capabilities in the dots. When the person asks something, answer in a way that moves them toward the relevant dot. When they are ready, surface the next thing they need from the chain. You are not a general assistant about Canadian bureaucracy. You are walking this specific person through the specific capabilities this situation requires.

RESPONSE FORMAT, THIS IS SPOKEN AUDIO:
- Talk like a calm, knowledgeable person sitting next to them. Plain speech.
- Keep responses short, a few sentences. They are listening, not reading.
- Never use bold, asterisks, bullets, dashes, lists, or headers.
- One idea at a time. Do not dump everything at once.

HOW YOU HELP:
- Meet the person where they are. If they are confused, slow down and build understanding before moving on.
- Use the reasoning inside each dot, the why and the how, so the person understands rather than memorizes.
- Check understanding lightly as you go, the way a person would, not like a quiz.
- When the situation is ready for it, you can offer to help them practice the interaction.

THE BOUNDARY, NON-NEGOTIABLE:
- You help the person understand, navigate, and prepare. You do not give medical, legal, or immigration advice.
- The representation includes boundary_flags. When the conversation reaches one of those flagged points, say so plainly and tell the person this is something to take to the right regulated professional, a lawyer, a doctor, or a regulated immigration consultant. Do not give the regulated advice yourself.

Never say dot, cluster, representation, or anything technical. To the person this is just a conversation about their situation.

THE SITUATION:

"""


# ── Sentence chunking (same as travel ask-stream) ──

def _has_tts_chunk(buffer: str) -> bool:
    return bool(re.search(r'[.!?]\s', buffer))


def _extract_tts_chunk(buffer: str):
    sentence_match = re.search(r'([.!?])\s', buffer)
    if sentence_match:
        end = sentence_match.end()
        return buffer[:end].strip(), buffer[end:]
    return buffer, ""


# ── Conversation endpoint (Phase 3) ──

@router.post("/converse-stream")
async def settlement_converse_stream(request: Request, student: dict = Depends(get_current_student)):
    """Settlement conversation anchored to a situation's dot representation."""
    body = await request.json()
    asset = body.get("asset")
    audio_b64_input = body.get("audio")
    text_question = body.get("text")
    history = body.get("history", [])

    if not asset or not isinstance(asset, dict):
        raise HTTPException(status_code=400, detail="asset is required")

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

    system_prompt = SETTLEMENT_CONVERSATION_PROMPT + json.dumps(asset, indent=2)

    api_messages = []
    for msg in history:
        if msg.get("role") and msg.get("content"):
            api_messages.append({"role": msg["role"], "content": msg["content"]})
    api_messages.append({"role": "user", "content": question})

    async def generate_stream():
        yield f"data: {json.dumps({'type': 'transcript', 'text': question})}\n\n"
        yield f"data: {json.dumps({'type': 'thinking'})}\n\n"

        client = anthropic.AsyncAnthropic()
        full_response = ""
        sentence_buffer = ""
        chunk_index = 0
        tts_client = httpx.AsyncClient(timeout=30.0)

        async def _tts(text_chunk: str, index: int):
            try:
                tts_response = await tts_client.post(
                    "https://api.inworld.ai/tts/v1/voice",
                    headers={
                        "Authorization": f"Basic {settings.INWORLD_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "text": text_chunk.strip(),
                        "voice_id": "Dennis",
                        "model_id": "inworld-tts-1.5-max",
                        "audio_config": {"audio_encoding": "MP3"},
                    },
                )
                if tts_response.status_code == 200:
                    tts_json = tts_response.json()
                    audio_b64 = tts_json.get("audioContent") or tts_json.get("result", {}).get("audioContent")
                    if audio_b64:
                        return f"data: {json.dumps({'type': 'audio_chunk', 'index': index, 'audio': audio_b64, 'format': 'mp3'})}\n\n"
                return f"data: {json.dumps({'type': 'tts_error', 'index': index, 'error': f'HTTP {tts_response.status_code}'})}\n\n"
            except Exception as e:
                logger.error(f"TTS chunk {index} failed: {e}")
                return f"data: {json.dumps({'type': 'tts_error', 'index': index, 'error': str(e)})}\n\n"

        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
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
                        yield await _tts(sentence, chunk_index)
                        chunk_index += 1

            if sentence_buffer.strip():
                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence_buffer})}\n\n"
                yield await _tts(sentence_buffer, chunk_index)

        except Exception as e:
            logger.error(f"Settlement conversation streaming failed: {e}")
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
