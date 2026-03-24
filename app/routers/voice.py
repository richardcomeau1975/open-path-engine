from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
import anthropic
import httpx
import json
import base64
import struct
import asyncio
from datetime import datetime

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
    ai_response = ai_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2048,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"}
        }],
        messages=api_messages,
    )

    ai_text = ai_response.content[0].text

    # Step 3: TTS
    async with httpx.AsyncClient() as client:
        tts_response = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={settings.GOOGLE_CLOUD_API_KEY}",
            json={
                "contents": [{"parts": [{"text": ai_text}]}],
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

    audio_response_b64 = None
    try:
        audio_response_b64 = tts_response.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    except (KeyError, IndexError):
        pass

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


@router.post("/podcast/{topic_id}/ask")
async def podcast_ask(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    audio_b64 = body.get("audio")
    text_question = body.get("text")

    supabase = get_supabase()

    # Step 1: Get the question text
    question = text_question
    if audio_b64 and not question:
        audio_bytes = base64.b64decode(audio_b64)
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

    # Step 2: Get learning asset
    topic = supabase.table("topics") \
        .select("learning_asset_url, podcast_script_url, course_id") \
        .eq("id", topic_id) \
        .execute()

    if not topic.data:
        raise HTTPException(404, "Topic not found")

    learning_asset = ""
    if topic.data[0].get("learning_asset_url"):
        try:
            asset_bytes = download_from_r2(topic.data[0]["learning_asset_url"])
            learning_asset = asset_bytes.decode("utf-8")
        except:
            pass

    # Step 3: Answer with Sonnet — in character as the podcast hosts
    # Also load the podcast script for context on what they were discussing
    podcast_script = ""
    if topic.data[0].get("podcast_script_url"):
        try:
            ps_bytes = download_from_r2(topic.data[0]["podcast_script_url"])
            podcast_script = ps_bytes.decode("utf-8")
        except:
            pass

    # Detect speaker names from script
    speaker_a = "HOST A"
    speaker_b = "HOST B"
    for line in podcast_script.split("\n")[:20]:
        line = line.strip()
        if ":" in line:
            name = line.split(":")[0].strip()
            if name and len(name) < 30 and not name.startswith("["):
                if speaker_a == "HOST A":
                    speaker_a = name
                elif name != speaker_a:
                    speaker_b = name
                    break

    system_prompt = (
        f"You are the two podcast hosts, {speaker_a} and {speaker_b}. The student just paused the podcast to ask you a question. "
        "Stay completely in character — same tone, same energy, same conversational dynamic between the two of you. "
        "Answer the question naturally as part of the conversation. You know this material deeply. "
        "If the question is about something tangential, connect it back to what you were discussing. "
        "If you genuinely don't know, say something like 'honestly that's a great question, I think it connects to...' and bridge to something you do know. "
        "NEVER say: learning asset, system, material provided, context, 'I don't have information on that', or anything that breaks the illusion that you're two real people having a conversation. "
        "NEVER refuse to answer. Always give the student something useful. "
        f"Write your response as dialogue between {speaker_a} and {speaker_b}, exactly like the podcast script format.\n\n"
        f"LEARNING ASSET:\n\n{learning_asset}\n\n"
    )
    if podcast_script:
        # Include last portion of script for context on what was being discussed
        script_tail = podcast_script[-4000:] if len(podcast_script) > 4000 else podcast_script
        system_prompt += f"RECENT PODCAST CONTEXT (what was being discussed when the student paused):\n\n{script_tail}"

    ai_client = anthropic.Anthropic()
    ai_response = ai_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": question}],
    )

    answer_text = ai_response.content[0].text

    # Step 4: Multi-speaker TTS matching podcast voices
    tts_prompt = f"TTS the following conversation between {speaker_a} and {speaker_b}:\n\n{answer_text}"

    audio_response_b64 = None
    async with httpx.AsyncClient() as client:
        tts_response = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-tts:generateContent?key={settings.GOOGLE_CLOUD_API_KEY}",
            json={
                "contents": [{"role": "user", "parts": [{"text": tts_prompt}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {
                        "multiSpeakerVoiceConfig": {
                            "speakerVoiceConfigs": [
                                {
                                    "speaker": speaker_a,
                                    "voiceConfig": {
                                        "prebuiltVoiceConfig": {"voiceName": "Kore"}
                                    }
                                },
                                {
                                    "speaker": speaker_b,
                                    "voiceConfig": {
                                        "prebuiltVoiceConfig": {"voiceName": "Puck"}
                                    }
                                }
                            ]
                        }
                    },
                    "temperature": 2.0,
                }
            },
            timeout=60.0,
        )

    try:
        audio_response_b64 = tts_response.json()["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    except (KeyError, IndexError):
        pass

    return {
        "transcript": question,
        "answer": answer_text,
        "audio": audio_response_b64,
    }
