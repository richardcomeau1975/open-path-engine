"""
Settlement domain routes.

generate: produces the dot-based card.
converse-stream: a conversation anchored to the card. Each turn streams a spoken
response to the voice, then emits a compact screen payload (an anchor line and
optional tappable points) parsed from a delimited tail.
"""

import asyncio
import base64
import json
import logging
import re
import uuid

import anthropic
import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from starlette.responses import StreamingResponse

from app.config import settings
from app.middleware.clerk_auth import get_current_student
from app.services.settlement_generator import generate_settlement_asset
from app.services.file_parser import parse_file
from app.services.r2 import upload_text_to_r2
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.tts import tts_chunk

logger = logging.getLogger(__name__)

class AnchorParser:
    def __init__(self):
        self.in_anchor = False
        self.anchor_buf = ""
        self.speak_buf = ""

    def feed(self, chunk):
        results = []
        self.speak_buf += chunk
        while True:
            if not self.in_anchor:
                idx = self.speak_buf.find("<<<ANCHOR>>>")
                if idx == -1:
                    break
                before = self.speak_buf[:idx]
                after = self.speak_buf[idx + 12:]
                if before:
                    results.append(("speak", before))
                self.speak_buf = ""
                self.anchor_buf = after
                self.in_anchor = True
            else:
                self.anchor_buf += self.speak_buf
                self.speak_buf = ""
                idx = self.anchor_buf.find("<<<END>>>")
                if idx == -1:
                    break
                content = self.anchor_buf[:idx]
                after = self.anchor_buf[idx + 9:]
                results.append(("anchor", content.strip()))
                self.anchor_buf = ""
                self.speak_buf = after
                self.in_anchor = False
        return results

    def flush(self):
        results = []
        if self.in_anchor and self.anchor_buf.strip():
            results.append(("anchor", self.anchor_buf.strip()))
        if self.speak_buf.strip():
            results.append(("speak", self.speak_buf.strip()))
        self.anchor_buf = ""
        self.speak_buf = ""
        return results


router = APIRouter(prefix="/api/settlement", tags=["settlement"])

DELIMITER = "###"


# ── Generator endpoint (unchanged) ──

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


# ── Document parse endpoint ──

@router.post("/parse-document")
async def settlement_parse_document(
    file: UploadFile = File(...),
    student: dict = Depends(get_current_student),
):
    """Parse an uploaded document into text for the generator."""
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        text = parse_file(file.filename or "", file_bytes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Document parse failed: {e}")
        raise HTTPException(status_code=502, detail="Could not parse the document")
    if not text or not text.strip():
        raise HTTPException(status_code=422, detail="No text could be extracted from the document")
    return {"text": text}


# ── MigrateEzy unified conversation prompt (used by both frame-stream and converse-stream) ──

MIGRATEEZY_CONVERSATION_PROMPT = """
A person has come to you for help with a situation they are facing in Canada. The material for their situation is provided below.

Your job is to genuinely help them with it. Understand what they actually need, and support them with that, meaningfully, in whatever way the conversation calls for. When something truly needs a lawyer or another professional, say so plainly.

Talk with them the way a good interviewer does, someone like Terry Gross: warm, genuinely curious, real. You are a person helping a person.
"""


def _get_conversation_prompt() -> str:
    try:
        return get_prompt_for_feature("migrateezy_conversation")
    except Exception:
        return MIGRATEEZY_CONVERSATION_PROMPT



# ── Sentence chunking ──

def _has_tts_chunk(buffer: str) -> bool:
    return bool(re.search(r'[.!?]\s', buffer))


def _extract_tts_chunk(buffer: str):
    sentence_match = re.search(r'([.!?])\s', buffer)
    if sentence_match:
        end = sentence_match.end()
        return buffer[:end].strip(), buffer[end:]
    return buffer, ""


# ── Screen tail parsing ──

def _parse_screen_tail(tail: str):
    """Extract the anchor line and points list from the post-delimiter tail."""
    anchor = ""
    points = []
    for line in tail.split("\n"):
        stripped = line.strip()
        upper = stripped.upper()
        if upper.startswith("ANCHOR:"):
            anchor = stripped[7:].strip()
        elif upper.startswith("POINTS:"):
            raw = stripped[7:].strip()
            points = [p.strip() for p in raw.split("|") if p.strip()]
    return anchor, points


# ── Conversation endpoint ──

@router.post("/converse-stream")
async def settlement_converse_stream(request: Request, student: dict = Depends(get_current_student)):
    """Settlement conversation anchored to a situation's card."""
    body = await request.json()
    asset = body.get("asset")
    audio_b64_input = body.get("audio")
    text_question = body.get("text")
    history = body.get("history", [])
    language = body.get("language", "en")

    if not asset or not isinstance(asset, dict):
        raise HTTPException(status_code=400, detail="asset is required")

    question = text_question
    if audio_b64_input and not question:
        audio_bytes = base64.b64decode(audio_b64_input)
        async with httpx.AsyncClient() as client:
            stt_response = await client.post(
                "https://api.deepgram.com/v1/listen",
                params={"model": "nova-3", "smart_format": "true", "language": language},
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

    system_prompt = _get_conversation_prompt() + "\n\n## THE REFERENCE MATERIAL FOR THIS SITUATION\n\n" + json.dumps(asset, indent=2)

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
        spoken_buffer = ""
        tail_buffer = ""
        delimiter_seen = False
        chunk_index = 0
        tts_client = httpx.AsyncClient(timeout=30.0)

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
                parser = AnchorParser()
                spoken_buffer = ""

                async for text in stream.text_stream:
                    full_response += text

                    for item_type, content in parser.feed(text):
                        if item_type == "anchor":
                            if spoken_buffer.strip():
                                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                                yield await tts_chunk(tts_client, spoken_buffer, chunk_index, language=language)
                                chunk_index += 1
                                spoken_buffer = ""
                            yield f"data: {json.dumps({'type': 'anchor', 'text': content})}\n\n"
                        elif item_type == "speak":
                            spoken_buffer += content

                    while re.search(r'[.!?]\s', spoken_buffer):
                        match = re.search(r'([.!?])\s', spoken_buffer)
                        end = match.end()
                        sentence = spoken_buffer[:end].strip()
                        spoken_buffer = spoken_buffer[end:]
                        if sentence:
                            yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence})}\n\n"
                            yield await tts_chunk(tts_client, sentence, chunk_index, language=language)
                            chunk_index += 1

                for item_type, content in parser.flush():
                    if item_type == "anchor":
                        yield f"data: {json.dumps({'type': 'anchor', 'text': content})}\n\n"
                    elif item_type == "speak" and content.strip():
                        spoken_buffer += content

            if not delimiter_seen and spoken_buffer.strip():
                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                yield await tts_chunk(tts_client, spoken_buffer, chunk_index, language=language)
                chunk_index += 1

            anchor, points = _parse_screen_tail(tail_buffer)
            yield f"data: {json.dumps({'type': 'screen', 'anchor': anchor, 'points': points})}\n\n"

        except Exception as e:
            logger.error(f"Settlement conversation streaming failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            await tts_client.aclose()

        spoken_only = full_response.split(DELIMITER, 1)[0].strip()
        yield f"data: {json.dumps({'type': 'answer', 'text': spoken_only})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


# ── Framing conversation endpoint ──

@router.post("/frame-stream")
async def settlement_frame_stream(request: Request, student: dict = Depends(get_current_student)):
    """Framing conversation. Runs while the card is generated in parallel. No card yet."""
    body = await request.json()
    situation_text = (body.get("situation_text") or "").strip()
    audio_b64_input = body.get("audio")
    text_question = body.get("text")
    history = body.get("history", [])
    language = body.get("language", "en")

    if not situation_text:
        raise HTTPException(status_code=400, detail="situation_text is required")

    question = text_question
    if audio_b64_input and not question:
        audio_bytes = base64.b64decode(audio_b64_input)
        async with httpx.AsyncClient() as client:
            stt_response = await client.post(
                "https://api.deepgram.com/v1/listen",
                params={"model": "nova-3", "smart_format": "true", "language": language},
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

    system_prompt = _get_conversation_prompt() + "\n\n## THE SITUATION IN FRONT OF THE PERSON\n\n" + situation_text

    api_messages = []
    for msg in history:
        if msg.get("role") and msg.get("content"):
            api_messages.append({"role": msg["role"], "content": msg["content"]})

    if question and question.strip():
        api_messages.append({"role": "user", "content": question})
    elif not api_messages:
        api_messages.append({"role": "user", "content": "I have just shared my situation. Please start."})
    else:
        api_messages.append({"role": "user", "content": "Please continue."})

    async def generate_stream():
        if question and question.strip():
            yield f"data: {json.dumps({'type': 'transcript', 'text': question})}\n\n"
        yield f"data: {json.dumps({'type': 'thinking'})}\n\n"

        client = anthropic.AsyncAnthropic()
        full_response = ""
        spoken_buffer = ""
        tail_buffer = ""
        delimiter_seen = False
        chunk_index = 0
        tts_client = httpx.AsyncClient(timeout=30.0)

        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=api_messages,
            ) as stream:
                parser = AnchorParser()
                spoken_buffer = ""

                async for text in stream.text_stream:
                    full_response += text

                    for item_type, content in parser.feed(text):
                        if item_type == "anchor":
                            if spoken_buffer.strip():
                                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                                yield await tts_chunk(tts_client, spoken_buffer, chunk_index, language=language)
                                chunk_index += 1
                                spoken_buffer = ""
                            yield f"data: {json.dumps({'type': 'anchor', 'text': content})}\n\n"
                        elif item_type == "speak":
                            spoken_buffer += content

                    while re.search(r'[.!?]\s', spoken_buffer):
                        match = re.search(r'([.!?])\s', spoken_buffer)
                        end = match.end()
                        sentence = spoken_buffer[:end].strip()
                        spoken_buffer = spoken_buffer[end:]
                        if sentence:
                            yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence})}\n\n"
                            yield await tts_chunk(tts_client, sentence, chunk_index, language=language)
                            chunk_index += 1

                for item_type, content in parser.flush():
                    if item_type == "anchor":
                        yield f"data: {json.dumps({'type': 'anchor', 'text': content})}\n\n"
                    elif item_type == "speak" and content.strip():
                        spoken_buffer += content

            if not delimiter_seen and spoken_buffer.strip():
                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                yield await tts_chunk(tts_client, spoken_buffer, chunk_index, language=language)
                chunk_index += 1

            anchor, points = _parse_screen_tail(tail_buffer)
            yield f"data: {json.dumps({'type': 'screen', 'anchor': anchor, 'points': points})}\n\n"

        except Exception as e:
            logger.error(f"Settlement framing streaming failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            await tts_client.aclose()

        spoken_only = full_response.split(DELIMITER, 1)[0].strip()
        yield f"data: {json.dumps({'type': 'answer', 'text': spoken_only})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
