import logging
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
import anthropic
import httpx
import json
import base64
import struct
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers
from app.services.r2 import download_from_r2
from app.config import settings

router = APIRouter(prefix="/api/voice", tags=["voice"])


@router.post("/transcribe")
async def transcribe_audio(request: Request, student: dict = Depends(get_current_student)):
    audio_bytes = await request.body()

    if not audio_bytes:
        raise HTTPException(400, "No audio data")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.deepgram.com/v1/listen",
            params={
                "model": "nova-3",
                "smart_format": "true",
                "language": "en",
            },
            headers={
                "Authorization": f"Token {settings.DEEPGRAM_API_KEY}",
                "Content-Type": "audio/webm",
            },
            content=audio_bytes,
            timeout=30.0,
        )

    if response.status_code != 200:
        raise HTTPException(502, f"Deepgram error: {response.status_code}")

    result = response.json()

    transcript = ""
    try:
        transcript = result["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError):
        pass

    return {"transcript": transcript}


@router.post("/speak")
async def text_to_speech(request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    text = body.get("text", "").strip()

    if not text:
        raise HTTPException(400, "No text provided")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={settings.GOOGLE_CLOUD_API_KEY}",
            json={
                "contents": [{"parts": [{"text": text}]}],
                "generationConfig": {
                    "response_modalities": ["AUDIO"],
                    "speech_config": {
                        "voiceConfig": {
                            "prebuiltVoiceConfig": {"voiceName": "Kore"}
                        }
                    }
                }
            },
            timeout=60.0,
        )

    if response.status_code != 200:
        raise HTTPException(502, f"Gemini TTS error: {response.status_code}")

    result = response.json()

    try:
        audio_b64 = result["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
        audio_bytes = base64.b64decode(audio_b64)
    except (KeyError, IndexError):
        raise HTTPException(502, "Failed to extract audio from Gemini response")

    # Raw PCM 16-bit 24kHz mono → WAV
    sample_rate = 24000
    num_channels = 1
    bits_per_sample = 16
    data_size = len(audio_bytes)
    header = struct.pack(
        '<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, num_channels, sample_rate,
        sample_rate * num_channels * bits_per_sample // 8,
        num_channels * bits_per_sample // 8, bits_per_sample,
        b'data', data_size,
    )
    wav_bytes = header + audio_bytes

    return StreamingResponse(
        iter([wav_bytes]),
        media_type="audio/wav",
        headers={"Content-Length": str(len(wav_bytes))},
    )


@router.post("/walkthrough/{topic_id}/voice-message")
async def voice_walkthrough_message(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    session_id = body.get("session_id")
    audio_b64 = body.get("audio")

    if not session_id or not audio_b64:
        raise HTTPException(400, "session_id and audio required")

    audio_bytes = base64.b64decode(audio_b64)

    # Step 1: Transcribe with Deepgram
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

    transcript = ""
    try:
        transcript = stt_response.json()["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError):
        raise HTTPException(502, "Transcription failed")

    if not transcript.strip():
        return {"transcript": "", "text": "", "audio": None}

    # Step 2: Get AI response
    supabase = get_supabase()

    session_result = supabase.table("walkthrough_sessions") \
        .select("*") \
        .eq("id", session_id) \
        .eq("student_id", student["id"]) \
        .execute()

    if not session_result.data:
        raise HTTPException(404, "Session not found")

    session = session_result.data[0]
    messages = session.get("messages", [])

    topic = supabase.table("topics").select("course_id, learning_asset_url").eq("id", topic_id).execute()
    course = supabase.table("courses").select("framework_type").eq("id", topic.data[0]["course_id"]).execute()
    framework_type = course.data[0].get("framework_type") if course.data else None
    course_id = topic.data[0]["course_id"]

    base_prompt = get_prompt_for_feature("walkthrough_tutor", framework_type)
    modifier_text = gather_modifiers(
        feature="walkthrough_tutor",
        student_id=student["id"],
        course_id=course_id,
        topic_id=topic_id,
    )

    learning_asset = ""
    if topic.data[0].get("learning_asset_url"):
        try:
            asset_bytes = download_from_r2(topic.data[0]["learning_asset_url"])
            learning_asset = asset_bytes.decode("utf-8")
        except:
            pass

    system_parts = [base_prompt]
    if modifier_text:
        system_parts.append(f"---\n\nMODIFIERS:\n\n{modifier_text}")
    if learning_asset:
        system_parts.append(f"---\n\nLEARNING ASSET:\n\n{learning_asset}")

    system_prompt = "\n\n".join(system_parts)
    system_prompt += f"\n\nSession mode: {session['mode']}"
    if session.get("cluster"):
        system_prompt += f"\nFocus cluster: {session['cluster']}"

    api_messages = [{"role": m["role"], "content": m["content"]} for m in messages]
    api_messages.append({"role": "user", "content": transcript})

    ai_client = anthropic.Anthropic()
    ai_response = await asyncio.to_thread(
        ai_client.messages.create,
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"}
        }],
        messages=api_messages,
    )

    ai_text = ai_response.content[0].text

    # Step 3: TTS via Inworld (Kelsey — same voice as lecture)
    from app.services.generators.tts import inworld_tts
    audio_response_b64 = None
    try:
        tts_result = await inworld_tts(ai_text, voice_id="Kelsey")
        if tts_result and tts_result.get("audio"):
            audio_response_b64 = tts_result["audio"]
    except Exception as e:
        logger.warning(f"Walkthrough TTS failed: {e}")

    # Save conversation
    messages.append({"role": "user", "content": transcript})
    messages.append({"role": "assistant", "content": ai_text})

    supabase.table("walkthrough_sessions").update({
        "messages": messages,
        "updated_at": datetime.utcnow().isoformat(),
    }).eq("id", session_id).execute()

    return {
        "transcript": transcript,
        "text": ai_text,
        "audio": audio_response_b64,
    }


# ── Filler audio generation (one-time admin endpoint) ────────────

@router.post("/podcast/generate-fillers")
async def generate_filler_clips(student: dict = Depends(get_current_student)):
    """One-time endpoint to generate all 16 filler audio clips and store on R2. Admin only."""
    if not student.get("is_admin"):
        raise HTTPException(403, "Admin only")

    from app.services.r2 import upload_bytes_to_r2
    from app.services.generators.tts import inworld_tts

    FILLERS = {
        "a": [
            "Oh — yeah yeah yeah, so that's actually the thing, right?",
            "OK so you're picking up on exactly what I was about to get into—",
            "Ha! I was literally just going to say something about that—",
            "Oh that's — yeah, that's the key question actually.",
        ],
        "b": [
            "Ooh. OK. That's interesting, let me think about that for a second...",
            "Hmm — you know what, that's a really good question actually...",
            "Oh wow, OK — so that's a different angle but it connects...",
            "That's — yeah. OK so here's the thing about that...",
        ],
        "c": [
            "Right right right — OK so let me back up for a sec...",
            "Oh — yeah, I probably should have been clearer about that...",
            "OK fair, let me put it differently—",
            "Yeah so — the way I think about it is...",
        ],
        "d": [
            "Oh — OK I see where you're going with that...",
            "Hm. That's fair actually. So here's the thing though—",
            "Yeah, no, that's a legitimate question...",
            "Interesting — so you're saying like, why would that be the case?",
        ],
    }

    results = []
    for category, lines in FILLERS.items():
        for i, line in enumerate(lines, 1):
            key = f"filler_audio/category_{category}_{i}.mp3"

            try:
                tts_result = await inworld_tts(line, voice_id="Kelsey")
                if tts_result and tts_result.get("audio"):
                    audio_data = base64.b64decode(tts_result["audio"])
                    upload_bytes_to_r2(key, audio_data, content_type="audio/mpeg")
                    results.append({"key": key, "status": "ok", "bytes": len(audio_data)})
                else:
                    results.append({"key": key, "status": "failed", "error": "no audio returned"})
            except Exception as e:
                results.append({"key": key, "status": "failed", "error": str(e)})

    return {"generated": len([r for r in results if r["status"] == "ok"]), "total": 16, "results": results}


# ── Filler audio URLs ────────────────────────────────────────────

@router.get("/podcast/filler-urls")
async def get_filler_urls(student: dict = Depends(get_current_student)):
    """Return presigned URLs for all 16 filler audio clips."""
    from app.services.r2 import generate_presigned_url
    categories = ["a", "b", "c", "d"]
    fillers = []
    for cat in categories:
        for i in range(1, 5):
            key = f"filler_audio/category_{cat}_{i}.mp3"
            try:
                url = generate_presigned_url(key)
                fillers.append({"key": key, "category": cat, "index": i, "url": url})
            except:
                pass
    return {"fillers": fillers}


# ── Podcast Q&A (streaming only) ──────────────────────────────────────

# ── True Streaming Q&A — Claude streams, Inworld TTS fires at sentence boundaries ──


def _has_tts_chunk(buffer: str) -> bool:
    """Check if buffer has a complete sentence or speaker turn to TTS."""
    import re
    if re.search(r'[.!?]\s', buffer):
        return True
    if re.search(r'\n\s*(HOST\s*[AB]|[A-Z][a-z]+)\s*:', buffer):
        return True
    return False


def _extract_tts_chunk(buffer: str) -> tuple:
    """Extract first complete sentence or speaker turn. Returns (chunk, remaining)."""
    import re
    speaker_match = re.search(r'\n\s*(?=(?:HOST\s*[AB]|[A-Z][a-z]+)\s*:)', buffer)
    if speaker_match and speaker_match.start() > 10:
        return buffer[:speaker_match.start()], buffer[speaker_match.start():]
    sentence_match = re.search(r'([.!?])\s', buffer)
    if sentence_match:
        end = sentence_match.end()
        return buffer[:end].strip(), buffer[end:]
    return buffer, ""


@router.post("/podcast/{topic_id}/ask-stream")
async def podcast_ask_stream(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    """Podcast Q&A with true streaming — Claude streams, Inworld TTS fires at sentence boundaries."""
    body = await request.json()
    audio_b64_input = body.get("audio")
    text_question = body.get("text")
    paused_at = body.get("pausedAt", 0)
    history = body.get("history", [])

    supabase = get_supabase()

    # Get question text
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

    # Load context
    topic = supabase.table("topics") \
        .select("learning_asset_url, podcast_script_url, course_id") \
        .eq("id", topic_id) \
        .execute()

    if not topic.data:
        raise HTTPException(404, "Topic not found")

    learning_asset = ""
    if topic.data[0].get("learning_asset_url"):
        try:
            learning_asset = download_from_r2(topic.data[0]["learning_asset_url"]).decode("utf-8")
        except:
            pass

    podcast_script = ""
    if topic.data[0].get("podcast_script_url"):
        try:
            podcast_script = download_from_r2(topic.data[0]["podcast_script_url"]).decode("utf-8")
        except:
            pass

    # Script context around pause
    script_context = ""
    if podcast_script and paused_at > 0:
        chars_per_second = 900 / 60
        estimated_position = int(paused_at * chars_per_second)
        start = max(0, estimated_position - 1000)
        end = min(len(podcast_script), estimated_position + 500)
        script_context = podcast_script[start:end]
    elif podcast_script:
        script_context = podcast_script[-2000:] if len(podcast_script) > 2000 else podcast_script

    # System prompt
    system_prompt = (
        "You are a knowledgeable, engaging teacher. The student just paused a lecture to ask you a question. "
        "Answer naturally and directly. You know this material deeply. "
        "Be warm, clear, and substantive. Speak as one person, not a dialogue. "
        "If the question is about something tangential, connect it back to the material. "
        "NEVER say: learning asset, system, material provided, context, or anything that breaks the illusion. "
        "NEVER refuse to answer. Always give the student something useful. "
        "Don't start with a filler reaction — the student already heard one. Jump straight into the answer. "
        "Answer in 3-4 sentences MAX. Be direct. One clear point per answer. The student will ask follow-ups if they want more — don't try to cover everything at once.\n\n"
        f"LEARNING ASSET:\n\n{learning_asset}\n\n"
    )
    if script_context:
        system_prompt += f"WHAT WAS BEING DISCUSSED WHEN THE STUDENT PAUSED:\n\n{script_context}"

    # Messages with history
    api_messages = []
    for msg in history:
        if msg.get("role") and msg.get("content"):
            api_messages.append({"role": msg["role"], "content": msg["content"]})
    api_messages.append({"role": "user", "content": question})

    # The streaming generator
    async def generate_stream():
        import re

        yield f"data: {json.dumps({'type': 'transcript', 'text': question})}\n\n"
        yield f"data: {json.dumps({'type': 'thinking'})}\n\n"

        # Stream Claude, buffer sentences, TTS each one via Inworld
        client = anthropic.AsyncAnthropic()

        full_response = ""
        sentence_buffer = ""
        chunk_index = 0
        tts_client = httpx.AsyncClient(timeout=30.0)

        try:
            async with client.messages.stream(
                model="claude-sonnet-4-6",
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
                                    "voice_id": "Kelsey",
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
                                    logger.warning(f"TTS chunk {chunk_index} — no audioContent in response")
                            else:
                                logger.warning(f"TTS chunk {chunk_index} failed: {tts_response.status_code} {tts_response.text[:200]}")
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
                            "voice_id": "Kelsey",
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
            logger.error(f"Streaming Q&A failed: {e}")
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
        finally:
            await tts_client.aclose()

        # Send full answer for history
        yield f"data: {json.dumps({'type': 'answer', 'text': full_response})}\n\n"
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        generate_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
