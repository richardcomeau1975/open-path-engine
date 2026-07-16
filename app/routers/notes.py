"""
Make My Notes — Station 5 (record).
One question at a time from the segment's dots; pushback until the answer holds;
accepted answers consolidate into student_notes; chart + markdown export.
"""

import asyncio
import json
import logging
from datetime import datetime, timezone

import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse

from app.config import settings
from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.r2 import download_from_r2
from app.routers.walkthrough import _extract_segment_from_asset

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/notes", tags=["notes"])

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 1200


def _load_segment_content(topic_id: str, segment_number: int, supabase) -> tuple[str, str]:
    """Segment content loader — same path and fallbacks as walkthrough segment_tutorial:
    segment YAML first, then full learning asset (extracting this segment when parseable).
    Returns (segment_content, course_id)."""
    topic_data = supabase.table("topics").select(
        "learning_asset_url, course_id"
    ).eq("id", topic_id).execute()

    if not topic_data.data:
        raise HTTPException(404, "Topic not found")

    learning_asset_url = topic_data.data[0].get("learning_asset_url")
    course_id = topic_data.data[0].get("course_id")

    content = ""
    try:
        content = download_from_r2(f"{topic_id}/segments/segment_{segment_number}.yaml").decode("utf-8")
    except Exception:
        pass

    if not content and learning_asset_url:
        try:
            content = download_from_r2(learning_asset_url).decode("utf-8")
            if content and len(content) > 3000:
                extracted = _extract_segment_from_asset(content, segment_number)
                if extracted:
                    content = extracted
        except Exception:
            pass

    if not content:
        raise HTTPException(404, "No learning asset available for this segment")

    return content, course_id


def _get_framework_type(supabase, course_id):
    if not course_id:
        return None
    course = supabase.table("courses").select("framework_type").eq("id", course_id).execute()
    return course.data[0].get("framework_type") if course.data else None


def _build_system_prompt(topic_id: str, segment_number: int, supabase) -> str:
    segment_content, course_id = _load_segment_content(topic_id, segment_number, supabase)
    framework_type = _get_framework_type(supabase, course_id)
    base_prompt = get_prompt_for_feature("note_maker", framework_type)
    return base_prompt + "\n\n---\n\nSEGMENT CONTENT:\n\n" + segment_content


def _parse_contract(raw: str) -> dict:
    """Parse the note_maker JSON contract, stripping fences defensively."""
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean[clean.index("\n") + 1:]
    if clean.endswith("```"):
        clean = clean[:-3]
    clean = clean.strip()
    try:
        parsed = json.loads(clean)
    except json.JSONDecodeError:
        start = clean.find("{")
        end = clean.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise HTTPException(502, "Note maker returned unparseable output")
        try:
            parsed = json.loads(clean[start:end + 1])
        except json.JSONDecodeError:
            raise HTTPException(502, "Note maker returned unparseable output")
    if not isinstance(parsed, dict) or "action" not in parsed or "text" not in parsed:
        raise HTTPException(502, "Note maker contract missing action/text")
    return parsed


async def _call_note_maker(system_prompt: str, api_messages: list) -> str:
    def _stream_sync() -> str:
        client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        out = ""
        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=api_messages,
        ) as stream:
            for text in stream.text_stream:
                out += text
        return out
    return await asyncio.to_thread(_stream_sync)


def _api_messages(messages: list) -> list:
    return [{"role": m["role"], "content": m["content"]} for m in messages]


def _current_question(messages: list):
    """Walk stored assistant turns in order; return the question the next note answers."""
    current = None
    for m in messages:
        if m["role"] != "assistant":
            continue
        parsed = m.get("parsed") or {}
        action = parsed.get("action")
        if action == "question":
            current = parsed.get("text")
        elif action == "consolidate":
            next_q = parsed.get("next_question")
            if next_q:
                current = next_q
    return current


def _contract_response(session_id, parsed: dict, inserted_note=None) -> dict:
    resp = {"session_id": session_id, "action": parsed["action"], "text": parsed["text"]}
    if "note" in parsed:
        resp["note"] = parsed["note"]
    if "next_question" in parsed:
        resp["next_question"] = parsed["next_question"]
    if inserted_note is not None:
        resp["inserted_note"] = inserted_note
    return resp


@router.post("/{topic_id}/start")
async def start_notes(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    segment_number = body.get("segment_number")
    if not segment_number:
        raise HTTPException(400, "segment_number required")

    supabase = get_supabase()
    system_prompt = _build_system_prompt(topic_id, int(segment_number), supabase)

    first_user = {"role": "user", "content": "Begin. Ask the first question."}
    raw = await _call_note_maker(system_prompt, _api_messages([first_user]))
    parsed = _parse_contract(raw)

    messages = [first_user, {"role": "assistant", "content": raw, "parsed": parsed}]
    result = supabase.table("note_sessions").insert({
        "topic_id": topic_id,
        "segment_number": int(segment_number),
        "student_id": student["id"],
        "messages": messages,
        "status": "active",
    }).execute()

    return _contract_response(result.data[0]["id"], parsed)


@router.post("/{topic_id}/message")
async def notes_message(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    body = await request.json()
    session_id = body.get("session_id")
    answer = (body.get("answer") or "").strip()
    if not session_id or not answer:
        raise HTTPException(400, "session_id and answer required")

    supabase = get_supabase()
    session_result = supabase.table("note_sessions").select("*") \
        .eq("id", session_id).eq("topic_id", topic_id).eq("student_id", student["id"]).execute()
    if not session_result.data:
        raise HTTPException(404, "Session not found")
    session = session_result.data[0]
    if session.get("status") == "complete":
        raise HTTPException(400, "Session already complete")

    messages = session.get("messages", [])
    system_prompt = _build_system_prompt(topic_id, session["segment_number"], supabase)

    messages.append({"role": "user", "content": answer})
    raw = await _call_note_maker(system_prompt, _api_messages(messages))
    parsed = _parse_contract(raw)

    inserted_note = None
    if parsed.get("action") == "consolidate" and parsed.get("note"):
        question = _current_question(messages) or ""
        note_row = supabase.table("student_notes").insert({
            "topic_id": topic_id,
            "segment_number": session["segment_number"],
            "student_id": student["id"],
            "question": question,
            "note": parsed["note"],
        }).execute()
        inserted_note = note_row.data[0] if note_row.data else None

    messages.append({"role": "assistant", "content": raw, "parsed": parsed})
    update = {"messages": messages, "updated_at": datetime.now(timezone.utc).isoformat()}
    if parsed.get("action") == "complete":
        update["status"] = "complete"
    supabase.table("note_sessions").update(update).eq("id", session_id).execute()

    return _contract_response(session_id, parsed, inserted_note)


@router.get("/{topic_id}/chart")
async def notes_chart(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    segment_number = request.query_params.get("segment_number")
    supabase = get_supabase()
    query = supabase.table("student_notes").select("*") \
        .eq("topic_id", topic_id).eq("student_id", student["id"])
    if segment_number:
        query = query.eq("segment_number", int(segment_number))
    result = query.order("segment_number").order("created_at").execute()
    return {"notes": result.data or []}


@router.get("/{topic_id}/export")
async def notes_export(topic_id: str, student: dict = Depends(get_current_student)):
    supabase = get_supabase()
    topic = supabase.table("topics").select("name").eq("id", topic_id).execute()
    topic_name = topic.data[0]["name"] if topic.data else "Topic"

    result = supabase.table("student_notes").select("*") \
        .eq("topic_id", topic_id).eq("student_id", student["id"]) \
        .order("segment_number").order("created_at").execute()

    lines = [f"# Notes — {topic_name}", ""]
    current_segment = None
    for n in (result.data or []):
        if n["segment_number"] != current_segment:
            current_segment = n["segment_number"]
            lines.append(f"## Lecture {current_segment}")
            lines.append("")
        lines.append(f"**Q:** {n['question']}")
        lines.append("")
        lines.append(n["note"])
        lines.append("")

    return PlainTextResponse(
        "\n".join(lines),
        media_type="text/markdown",
        headers={"Content-Disposition": 'attachment; filename="notes.md"'},
    )
