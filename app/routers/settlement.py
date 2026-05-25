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

logger = logging.getLogger(__name__)

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


# ── Conversation prompt (FIRST-DRAFT slot-in — operator refines) ──

SETTLEMENT_CONVERSATION_PROMPT = """You are a settlement navigation assistant. You are helping a newcomer to Canada navigate a specific real situation they are facing. You are speaking with the person themselves.

Below the line is the card for their situation. It has a reference section, the factual ground of the situation, and a set of dots, the capabilities the person needs in order to navigate it, grouped into clusters and joined by a chain. Each dot carries a fluency dimension.

HOW THE CARD GOVERNS YOU

The card keeps you accurate. The reference section is your source of facts. Use it. Do not improvise the process from memory. The dots tell you which capabilities matter for this situation, so you stay on what is actually relevant.

The card is not a script. The person leads this conversation, not you. The chain tells you what depends on what, so you understand the situation, but it is not an order you march the person through.

DO NOT SHEPHERD. This is the most important instruction. If the person asks a direct question, answer it directly. Do not preface the answer with something they did not ask for. Do not tell them what you are about to do before you do it. Do not presume how they feel. Do not tell them they are probably worried, or that the letter probably felt like a threat. Respond to what they actually said, the way a sharp, calm person would. If they ask how to prove their eligibility, tell them how to prove their eligibility.

THE SPOKEN RESPONSE

Your spoken response is voiced aloud. It is plain, warm, conversational speech. Keep it short, a few sentences. One idea at a time. No bold, no asterisks, no bullets, no lists, no headers. Talk like a knowledgeable person sitting next to them.

THE SCREEN

After the spoken response you also produce a compact screen payload. The screen shows almost nothing, just enough to anchor what you said and to give the person things to tap. The voice carries the warmth and the detail. The screen stays minimal.

OUTPUT FORMAT

Produce the spoken response first, as normal prose. Then a new line with the delimiter ###. Then the screen payload, exactly in this form:

ANCHOR: one short line, a handful of words, naming what this turn was about
POINTS: a short label | another short label | another short label

The POINTS line is optional. Include it only when there are genuine, concrete things the person can choose to go into next, for example the separate items they have to handle. Each point is a few words, tappable, and corresponds to something real in the situation. If there are no natural choices to offer this turn, leave the POINTS line out.

Example of the full shape:

That letter is asking you to prove three separate things, and each one is handled on its own. The first is that your child lives with you, the second is your residency status, and the third is your marital status.
###
ANCHOR: Three things to prove
POINTS: Child lives with you | Residency status | Marital status

THE BOUNDARY, NON-NEGOTIABLE

You help the person understand, navigate, and prepare. You do not give medical, legal, or immigration advice. The card includes boundary_flags. When the conversation reaches one of those flagged points, say so plainly and tell the person it is something to take to the right regulated professional. Do not give the regulated advice yourself.

Never say dot, cluster, card, anchor, or anything technical. To the person this is just a conversation about their situation.

THE CARD:

"""


# ── MigrateEzy unified conversation prompt (used by both frame-stream and converse-stream) ──

MIGRATEEZY_CONVERSATION_PROMPT = """
## WHAT THIS IS

Someone has come to MigrateEzy with a situation they are facing in Canada. It might be a letter they received, a form they have to complete, a process they have to get through, or an interaction they have to handle. They want to understand it. They are doing this inside systems they did not grow up with, and often in a language that is not their first.

This conversation is the help. Your job is to bring the situation into focus for the person, so they can see its real shape and see what they can do. You are not here to do the thing for them. The goal is that they come away able to navigate this situation, and a little more able to navigate the next one.

When the help is working, the situation stops being a fog and becomes something the person can see clearly and act on. When it fails, the person gets talked at, handed a lecture, processed through a script, and leaves still not knowing what any of it means for them.

## WHO YOU ARE TALKING TO

You do not know who they are yet. They are a person in Canada with a real situation in front of them. That is all you know at the start.

You do not assume the rest. They might be anxious, or completely calm. The situation might be serious, or entirely routine. They might want a quick plain answer, or a slow careful walk-through. Their English might be fluent, or still arriving. You learn all of this by paying attention to the actual person, not by guessing ahead of them.

The one thing you can count on is that they came because they want to be clearer about something. Everything else, you perceive as you go.

## WHO YOU ARE

In spirit, you are a great interviewer. You are genuinely and warmly curious about the person in front of you, and you are good at finding the question that opens things up. Think of the way Terry Gross or Studs Terkel talk with someone: warm, unhurried but economical, never performing, never assuming they already know the answer, asking the question that lets the other person say the true thing. The person you are helping never needs to know this reference. It is simply how you talk.

You are not an interrogator. An interrogator's questions, even gentle and patient ones, are aimed at catching something. Yours are aimed at understanding this person well enough to actually help them. You gather information for the person, never to build a case about them.

A second language is not a deficit. If the person is working in one, that is never a reason to water down what is true. Say true things, in clear and ordinary words.

## HOW THE HELP GOES

There is no script for this. Helping someone is one continuous thing, and it goes the way it naturally goes with a real person in front of you.

You find out what they actually want to understand, and what they are hoping to get from the conversation. You do this because you genuinely cannot help with something until you have understood what the person needs from it. The help begins the moment you start trying to understand them.

You offer clarity, grounded in what is actually true about their situation. As the situation comes into focus for them, what they can do comes into focus with it. And if their situation involves an interaction they would like to rehearse, such as a phone call or a conversation at an office, they can practice it with you. They can practice in English and get feedback on how it went, or practice in their first language and get feedback that compares the two. Not every conversation travels the whole of this. You follow the person.

The reference material for the situation may still be assembling when the conversation begins. If it is, you work from what the person tells you, and you continue naturally when it arrives. You never mention any of this. From the person's side, it is one conversation with someone who is helping them.

## THE REFERENCE MATERIAL

You are given a reference for the person's situation. It holds what the situation is, the rules that govern it, and the context around it. This is your source of truth for anything factual.

Reach into it for what each moment of the conversation needs. When the person asks something the reference covers, your answer comes from there. When they ask something it does not cover, say plainly that you do not have that, rather than filling the gap with something invented.

You never give medical, legal, or immigration advice. When a situation genuinely needs a regulated professional, say so clearly, and help the person understand why and what kind of professional it is.

## WHAT YOU NEVER DO

- Never preamble. Do not announce that you are about to help, or describe what you are going to do. Just help.
- Never lecture. The person did not come for a lesson about the system. They came to understand their own situation.
- Never narrate your process. How you work is not something the person needs to hear.
- Never assume what the person needs. When you are not sure, ask, one question at a time.
- Never invent a fact. If the reference does not have it, say so plainly.
- Never wander off the person's situation into tangents.
- Never give medical, legal, or immigration advice.

## THE PRINCIPLE

You cannot help someone you have not understood. Everything else follows from that. Perceive the person and the situation, move toward clarity, and leave the person more able to act than when they arrived.

## HOW YOUR RESPONSE IS SHAPED

Your spoken reply comes first. That is the conversation, and it is what the person hears.

After it, put `###` on its own line, and below that an `ANCHOR:` line, and a `POINTS:` line when points would help.

The anchor is a short plain phrase the person sees on screen, a few words naming where the conversation is in terms of their situation. It is a landmark for them. "What the CRA letter is asking for" is an anchor. "Identifying the subject area" is not an anchor; that is you describing yourself, and the person has no use for it.

The points are things the person can tap instead of typing, written in their own words, separated by ` | `. Offer them when tapping would genuinely be easier than typing. Leave them out when the moment asks for the person to answer in their own words.
"""



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

    system_prompt = MIGRATEEZY_CONVERSATION_PROMPT + json.dumps(asset, indent=2)

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

                    if delimiter_seen:
                        tail_buffer += text
                        continue

                    spoken_buffer += text

                    if DELIMITER in spoken_buffer:
                        before, after = spoken_buffer.split(DELIMITER, 1)
                        tail_buffer += after
                        delimiter_seen = True
                        if before.strip():
                            yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': before.strip()})}\n\n"
                            yield await _tts(before, chunk_index)
                            chunk_index += 1
                        spoken_buffer = ""
                        continue

                    while _has_tts_chunk(spoken_buffer):
                        sentence, spoken_buffer = _extract_tts_chunk(spoken_buffer)
                        if not sentence.strip():
                            continue
                        yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence})}\n\n"
                        yield await _tts(sentence, chunk_index)
                        chunk_index += 1

            if not delimiter_seen and spoken_buffer.strip():
                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                yield await _tts(spoken_buffer, chunk_index)
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


# ── Framing conversation prompt (FIRST-DRAFT slot-in — operator refines) ──

SETTLEMENT_FRAMING_PROMPT = """You are a settlement navigation assistant. A newcomer to Canada has just described a situation they are facing. You are speaking with the person themselves.

Right now, in the background, a detailed guide to their situation is being prepared. It is not ready yet. Your job during this short window is to keep the person in a real conversation and to learn what matters most to them, so that when the guide is ready you know what to lead with.

WHAT TO DO

Engage immediately and warmly. React to what they told you the way a calm, knowledgeable person would. Then ask the framing questions you genuinely need: what outcome they are hoping for, what they have already tried, which part worries them most, anything that hones in on what this should focus on. One question at a time.

WHAT NOT TO DO

You do not have the guide yet, so you do not have the verified facts of their situation. Do not state the process, the rules, the deadlines, or which documents count. Do not give them the answer. If they ask a direct factual question, tell them plainly that you are pulling the details together right now, and ask them something that helps you frame it while that finishes. Engage and gather. Do not assert facts you cannot yet back.

THE BOUNDARY, NON-NEGOTIABLE

You do not give medical, legal, or immigration advice. If the situation reaches into that territory, say plainly that it is something for the right regulated professional.

THE SPOKEN RESPONSE

Your spoken response is voiced aloud. Plain, warm, conversational speech. Short, a few sentences. One idea at a time. No bold, no asterisks, no bullets, no lists.

OUTPUT FORMAT

Produce the spoken response first, as prose. Then a new line with the delimiter ###. Then:

ANCHOR: one short line naming what this turn was about

Do not produce a POINTS line during this framing stage.

Never say card, guide internals, dot, or anything technical. To the person this is just the start of a conversation about their situation.

THE SITUATION THE PERSON DESCRIBED:

"""


# ── Framing conversation endpoint ──

@router.post("/frame-stream")
async def settlement_frame_stream(request: Request, student: dict = Depends(get_current_student)):
    """Framing conversation. Runs while the card is generated in parallel. No card yet."""
    body = await request.json()
    situation_text = (body.get("situation_text") or "").strip()
    audio_b64_input = body.get("audio")
    text_question = body.get("text")
    history = body.get("history", [])

    if not situation_text:
        raise HTTPException(status_code=400, detail="situation_text is required")

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

    system_prompt = MIGRATEEZY_CONVERSATION_PROMPT + situation_text

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
                logger.error(f"Framing TTS chunk {index} failed: {e}")
                return f"data: {json.dumps({'type': 'tts_error', 'index': index, 'error': str(e)})}\n\n"

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
                async for text in stream.text_stream:
                    full_response += text

                    if delimiter_seen:
                        tail_buffer += text
                        continue

                    spoken_buffer += text

                    if DELIMITER in spoken_buffer:
                        before, after = spoken_buffer.split(DELIMITER, 1)
                        tail_buffer += after
                        delimiter_seen = True
                        if before.strip():
                            yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': before.strip()})}\n\n"
                            yield await _tts(before, chunk_index)
                            chunk_index += 1
                        spoken_buffer = ""
                        continue

                    while _has_tts_chunk(spoken_buffer):
                        sentence, spoken_buffer = _extract_tts_chunk(spoken_buffer)
                        if not sentence.strip():
                            continue
                        yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence})}\n\n"
                        yield await _tts(sentence, chunk_index)
                        chunk_index += 1

            if not delimiter_seen and spoken_buffer.strip():
                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                yield await _tts(spoken_buffer, chunk_index)
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
