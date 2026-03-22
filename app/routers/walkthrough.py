from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
import anthropic
import json
from datetime import datetime

from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers
from app.services.r2 import download_from_r2

router = APIRouter(prefix="/api/walkthrough", tags=["walkthrough"])


@router.get("/{topic_id}/sessions")
async def get_sessions(topic_id: str, student: dict = Depends(get_current_student)):
    supabase = get_supabase()

    result = supabase.table("walkthrough_sessions") \
        .select("id, mode, cluster, is_active, created_at, updated_at, messages") \
        .eq("topic_id", topic_id) \
        .eq("student_id", student["id"]) \
        .order("updated_at", desc=True) \
        .execute()

    sessions = []
    for s in result.data:
        msg_count = len(s.get("messages", []))
        sessions.append({
            "id": s["id"],
            "mode": s["mode"],
            "cluster": s["cluster"],
            "is_active": s["is_active"],
            "message_count": msg_count,
            "last_message_preview": s["messages"][-1]["content"][:100] if msg_count > 0 else None,
            "created_at": s["created_at"],
            "updated_at": s["updated_at"],
        })

    return {"sessions": sessions}


@router.post("/{topic_id}/start")
async def start_session(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    mode = body.get("mode", "foundation")  # "foundation" or "application"
    cluster = body.get("cluster")  # optional — specific cluster to focus on
    session_id = body.get("session_id")  # optional — resume existing session

    supabase = get_supabase()

    if session_id:
        # Resume existing session
        result = supabase.table("walkthrough_sessions") \
            .select("*") \
            .eq("id", session_id) \
            .eq("student_id", student["id"]) \
            .execute()
        if not result.data:
            raise HTTPException(404, "Session not found")
        return {"session": result.data[0]}

    # Create new session
    result = supabase.table("walkthrough_sessions").insert({
        "topic_id": topic_id,
        "student_id": student["id"],
        "mode": mode,
        "cluster": cluster,
        "messages": [],
        "is_active": True,
    }).execute()

    return {"session": result.data[0]}


@router.post("/{topic_id}/message")
async def send_message(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    session_id = body.get("session_id")
    student_message = body.get("message", "").strip()

    if not session_id or not student_message:
        raise HTTPException(400, "session_id and message required")

    supabase = get_supabase()

    # Load session
    session_result = supabase.table("walkthrough_sessions") \
        .select("*") \
        .eq("id", session_id) \
        .eq("student_id", student["id"]) \
        .execute()

    if not session_result.data:
        raise HTTPException(404, "Session not found")

    session = session_result.data[0]
    messages = session.get("messages", [])

    # Get course info for framework type
    topic = supabase.table("topics").select("course_id").eq("id", topic_id).execute()
    course = supabase.table("courses").select("framework_type, student_id").eq("id", topic.data[0]["course_id"]).execute()
    framework_type = course.data[0].get("framework_type") if course.data else None
    course_id = topic.data[0]["course_id"]

    # Build the system prompt
    base_prompt = get_prompt_for_feature("walkthrough_tutor", framework_type)

    modifier_text = gather_modifiers(
        feature="walkthrough_tutor",
        student_id=student["id"],
        course_id=course_id,
        topic_id=topic_id,
    )

    # Load the learning asset
    topic_data = supabase.table("topics").select("learning_asset_url").eq("id", topic_id).execute()
    learning_asset_url = topic_data.data[0].get("learning_asset_url") if topic_data.data else None

    learning_asset = ""
    if learning_asset_url:
        try:
            asset_bytes = download_from_r2(learning_asset_url)
            learning_asset = asset_bytes.decode("utf-8")
        except:
            pass

    # Assemble system prompt
    system_parts = [base_prompt]
    if modifier_text:
        system_parts.append(f"---\n\nMODIFIERS:\n\n{modifier_text}")
    if learning_asset:
        system_parts.append(f"---\n\nLEARNING ASSET:\n\n{learning_asset}")

    system_prompt = "\n\n".join(system_parts)

    # Add mode context
    mode_context = f"\n\nSession mode: {session['mode']}"
    if session.get("cluster"):
        mode_context += f"\nFocus cluster: {session['cluster']}"
    system_prompt += mode_context

    # Build conversation messages for API
    api_messages = []
    for msg in messages:
        api_messages.append({"role": msg["role"], "content": msg["content"]})
    api_messages.append({"role": "user", "content": student_message})

    # Call Sonnet with streaming + prompt caching
    client = anthropic.Anthropic()

    async def stream_response():
        full_response = ""
        try:
            with client.messages.stream(
                model="claude-sonnet-4-20250514",
                max_tokens=2048,
                system=[{
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"}
                }],
                messages=api_messages,
            ) as stream:
                for text in stream.text_stream:
                    full_response += text
                    yield f"data: {json.dumps({'type': 'text', 'text': text})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n"
            return

        # Save conversation to session
        messages.append({"role": "user", "content": student_message})
        messages.append({"role": "assistant", "content": full_response})

        supabase.table("walkthrough_sessions").update({
            "messages": messages,
            "updated_at": datetime.utcnow().isoformat(),
        }).eq("id", session_id).execute()

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )
