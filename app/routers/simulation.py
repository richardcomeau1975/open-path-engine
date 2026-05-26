"""
Settlement simulation routes.

sim/brief:    produces a scenario brief from the card
sim/turn:     streams one roleplay turn (counterpart in character)
sim/evaluate: evaluates a single-language transcript
sim/bridge:   cross-language comparison of two transcripts
"""

import json
import logging
import base64
import re

import anthropic
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.middleware.clerk_auth import get_current_student
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.tts import tts_chunk

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settlement/sim", tags=["settlement-simulation"])


def _load_prompt(feature: str) -> str:
    return get_prompt_for_feature(feature)


def _has_tts_chunk(buffer: str) -> bool:
    return bool(re.search(r'[.!?]\s', buffer))


def _extract_tts_chunk(buffer: str):
    sentence_match = re.search(r'([.!?])\s', buffer)
    if sentence_match:
        end = sentence_match.end()
        return buffer[:end].strip(), buffer[end:]
    return buffer, ""


@router.post("/brief")
async def sim_brief(request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    card = body.get("card")

    if not card or not isinstance(card, dict):
        raise HTTPException(status_code=400, detail="card is required")

    system_prompt = _load_prompt("migrateezy_sim_brief")

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": "Here is the card:\n\n" + json.dumps(card, indent=2) + "\n\nProduce the scenario brief now. Output only the JSON.",
            }],
        )
        text = response.content[0].text.strip()
        brief = json.loads(text)
        return {"brief": brief}
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Brief generator did not return valid JSON")
    except Exception as e:
        logger.error(f"Brief generation failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/turn")
async def sim_turn(request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    brief = body.get("brief")
    audio_b64_input = body.get("audio")
    text_input = body.get("text")
    history = body.get("history", [])
    language = body.get("language", "en")

    if not brief or not isinstance(brief, dict):
        raise HTTPException(status_code=400, detail="brief is required")

    question = text_input
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

    counterpart_prompt = _load_prompt("migrateezy_sim_counterpart")
    system_prompt = counterpart_prompt + "\n\n## THE SCENARIO BRIEF\n\n" + json.dumps(brief, indent=2)

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
        chunk_index = 0
        tts_client = httpx.AsyncClient(timeout=30.0)

        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=api_messages,
            ) as stream:
                async for text in stream.text_stream:
                    full_response += text
                    spoken_buffer += text

                    while _has_tts_chunk(spoken_buffer):
                        sentence, spoken_buffer = _extract_tts_chunk(spoken_buffer)
                        if not sentence.strip():
                            continue
                        yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': sentence})}\n\n"
                        yield await tts_chunk(tts_client, sentence, chunk_index, language=language)
                        chunk_index += 1

            if spoken_buffer.strip():
                yield f"data: {json.dumps({'type': 'text_chunk', 'index': chunk_index, 'text': spoken_buffer.strip()})}\n\n"
                yield await tts_chunk(tts_client, spoken_buffer, chunk_index, language=language)
                chunk_index += 1

        except Exception as e:
            logger.error(f"Simulation turn streaming failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            await tts_client.aclose()

        yield f"data: {json.dumps({'type': 'answer', 'text': full_response.strip()})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@router.post("/evaluate")
async def sim_evaluate(request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    transcript = body.get("transcript")
    ground_truth = body.get("ground_truth")
    language = body.get("language", "en")

    if not transcript:
        raise HTTPException(status_code=400, detail="transcript is required")
    if not ground_truth:
        raise HTTPException(status_code=400, detail="ground_truth is required")

    eval_prompt = _load_prompt("migrateezy_sim_evaluate")

    user_content = (
        "## TRANSCRIPT\n\n"
        + (json.dumps(transcript, indent=2) if isinstance(transcript, list) else str(transcript))
        + "\n\n## GROUND TRUTH\n\n"
        + json.dumps(ground_truth, indent=2)
        + "\n\nLanguage of this transcript: " + language
        + "\n\nEvaluate now. Output only the JSON."
    )

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=eval_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        evaluation = json.loads(text)
        return {"evaluation": evaluation}
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Evaluator did not return valid JSON")
    except Exception as e:
        logger.error(f"Evaluation failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


@router.post("/bridge")
async def sim_bridge(request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    l1_transcript = body.get("l1_transcript")
    en_transcript = body.get("en_transcript")
    ground_truth = body.get("ground_truth")
    l1_language = body.get("l1_language", "unknown")

    if not l1_transcript:
        raise HTTPException(status_code=400, detail="l1_transcript is required")
    if not en_transcript:
        raise HTTPException(status_code=400, detail="en_transcript is required")
    if not ground_truth:
        raise HTTPException(status_code=400, detail="ground_truth is required")

    bridge_prompt = _load_prompt("migrateezy_sim_bridge")

    user_content = (
        "## FIRST-LANGUAGE TRANSCRIPT (" + l1_language + ")\n\n"
        + (json.dumps(l1_transcript, indent=2) if isinstance(l1_transcript, list) else str(l1_transcript))
        + "\n\n## ENGLISH TRANSCRIPT\n\n"
        + (json.dumps(en_transcript, indent=2) if isinstance(en_transcript, list) else str(en_transcript))
        + "\n\n## GROUND TRUTH\n\n"
        + json.dumps(ground_truth, indent=2)
        + "\n\nProduce the bridge evaluation now. Output only the JSON."
    )

    client = anthropic.Anthropic()
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=bridge_prompt,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        bridge = json.loads(text)
        return {"bridge": bridge}
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="Bridge evaluator did not return valid JSON")
    except Exception as e:
        logger.error(f"Bridge evaluation failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))
