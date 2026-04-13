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
        .select("id, mode, cluster_index, completion_state, created_at, updated_at, messages") \
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
            "cluster": s["cluster_index"],
            "is_active": s.get("completion_state") == "in_progress",
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
    session_data = {
        "topic_id": topic_id,
        "student_id": student["id"],
        "mode": mode,
        "cluster_index": cluster,
        "messages": [],
        "completion_state": "in_progress",
    }

    # Segment tutorial: store segment number in metadata
    if mode == "segment_tutorial":
        segment_number = body.get("segment_number", 1)
        session_data["metadata"] = {"segment_number": segment_number}

    # Admin can provide a test prompt for this session
    test_prompt = body.get("test_prompt")
    if test_prompt and student.get("is_admin"):
        session_data.setdefault("metadata", {})["test_prompt"] = test_prompt

    result = supabase.table("walkthrough_sessions").insert(session_data).execute()

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

    # Use test prompt if stored in session, otherwise use the real prompt
    session_metadata = session.get("metadata") or {}
    test_prompt = session_metadata.get("test_prompt")

    if test_prompt:
        base_prompt = test_prompt
    else:
        base_prompt = get_prompt_for_feature("walkthrough_tutor", framework_type)

    modifier_text = gather_modifiers(
        feature="walkthrough_tutor",
        student_id=student["id"],
        course_id=course_id,
        topic_id=topic_id,
    )

    # Load the learning asset (segment-specific or full)
    topic_data = supabase.table("topics").select("learning_asset_url").eq("id", topic_id).execute()
    learning_asset_url = topic_data.data[0].get("learning_asset_url") if topic_data.data else None

    learning_asset = ""

    # For segment tutorials, try to load segment-specific content
    if session["mode"] == "segment_tutorial":
        segment_num = (session.get("metadata") or {}).get("segment_number", 1)
        try:
            seg_asset = download_from_r2(f"{topic_id}/segments/segment_{segment_num}.yaml").decode("utf-8")
            learning_asset = seg_asset
        except Exception:
            pass  # Fall through to full asset

        # Append anchor context from lecture manifest
        try:
            import json as _json
            manifest = _json.loads(download_from_r2(f"{topic_id}/lecture/manifest.json").decode("utf-8"))
            seg_info = manifest["segments"][segment_num - 1]
            anchors = seg_info.get("anchors", [])
            if anchors:
                learning_asset += "\n\n# KEY MOMENTS FROM THE LECTURE\nThe student just heard a lecture where these ideas crystallized:\n"
                for anchor in anchors:
                    learning_asset += f"- {anchor}\n"
                learning_asset += "\nThe tutorial should test whether the student genuinely arrived at these understandings — not by asking what the lecture said, but by making them USE the ideas.\n"
        except Exception:
            pass

    # Fallback to full learning asset if nothing loaded yet
    if not learning_asset and learning_asset_url:
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
    if session.get("cluster_index"):
        mode_context += f"\nFocus cluster: {session['cluster_index']}"
    # Check metadata for gaps context
    session_meta = session.get("metadata") or {}
    if session_meta.get("gaps_context"):
        mode_context += f"\n{session_meta['gaps_context']}"
    system_prompt += mode_context

    # Build conversation messages for API
    api_messages = []
    for msg in messages:
        api_messages.append({"role": msg["role"], "content": msg["content"]})
    api_messages.append({"role": "user", "content": student_message})

    # Call Sonnet with streaming + prompt caching
    client = anthropic.AsyncAnthropic()

    async def stream_response():
        full_response = ""
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


@router.post("/{topic_id}/start-gaps")
async def start_gaps_session(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    supabase = get_supabase()

    # Get fuzzy concepts from verifier results
    fuzzy = supabase.table("verifier_results") \
        .select("question, got, missing") \
        .eq("topic_id", topic_id) \
        .eq("student_id", student["id"]) \
        .eq("status", "fuzzy") \
        .execute()

    if not fuzzy.data:
        return {"session": None, "message": "No gaps to work on — everything is solid."}

    # Build a scope description for the AI
    gaps_context = "FOCUS ON THESE SPECIFIC GAPS:\n\n"
    for item in fuzzy.data:
        gaps_context += f"Question: {item['question']}\n"
        gaps_context += f"What the student got: {item['got']}\n"
        gaps_context += f"What's missing: {item['missing']}\n\n"

    # Create session with mode='gaps' and the gaps context as cluster
    result = supabase.table("walkthrough_sessions").insert({
        "topic_id": topic_id,
        "student_id": student["id"],
        "mode": "gaps",
        "cluster_index": None,
        "messages": [],
        "completion_state": "in_progress",
        "metadata": {"gaps_context": gaps_context},
    }).execute()

    return {"session": result.data[0], "gaps": fuzzy.data}


@router.post("/{topic_id}/resolve-gap")
async def resolve_gap(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    question = body.get("question")

    if not question:
        raise HTTPException(400, "question required")

    supabase = get_supabase()

    # Update verifier result from fuzzy to solid
    supabase.table("verifier_results") \
        .update({"status": "solid", "missing": None, "updated_at": datetime.utcnow().isoformat()}) \
        .eq("topic_id", topic_id) \
        .eq("student_id", student["id"]) \
        .eq("question", question) \
        .execute()

    return {"status": "resolved"}


@router.get("/{topic_id}/progress")
async def get_progress(topic_id: str, student: dict = Depends(get_current_student)):
    supabase = get_supabase()

    # Walkthrough sessions
    sessions = supabase.table("walkthrough_sessions") \
        .select("id, mode, completion_state, messages, created_at, updated_at") \
        .eq("topic_id", topic_id) \
        .eq("student_id", student["id"]) \
        .order("updated_at", desc=True) \
        .execute()

    # Verifier results
    verifier = supabase.table("verifier_results") \
        .select("question, status, got, missing") \
        .eq("topic_id", topic_id) \
        .eq("student_id", student["id"]) \
        .execute()

    # Note chart answers
    answers = supabase.table("note_chart_answers") \
        .select("question, answer") \
        .eq("topic_id", topic_id) \
        .eq("student_id", student["id"]) \
        .execute()

    total_questions = len(answers.data) if answers.data else 0
    answered = len([a for a in (answers.data or []) if a.get("answer", "").strip()])

    solid = len([v for v in (verifier.data or []) if v["status"] == "solid"])
    fuzzy = len([v for v in (verifier.data or []) if v["status"] == "fuzzy"])

    walkthrough_sessions_count = len(sessions.data) if sessions.data else 0
    total_messages = sum(len(s.get("messages", [])) for s in (sessions.data or []))

    return {
        "active_recall": {
            "total_questions": total_questions,
            "answered": answered,
            "evaluated": solid + fuzzy > 0,
            "solid": solid,
            "fuzzy": fuzzy,
        },
        "walkthrough": {
            "sessions": walkthrough_sessions_count,
            "total_exchanges": total_messages // 2,
            "has_active_session": any(s.get("completion_state") == "in_progress" for s in (sessions.data or [])),
        },
    }
