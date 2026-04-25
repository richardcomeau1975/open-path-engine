"""
Exit Ticket — segment competency gate.
Generates tasks, evaluates responses, gates progression.
"""

import json
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, HTTPException
import anthropic

from app.services.supabase import get_supabase
from app.middleware.clerk_auth import get_current_student
from app.services.r2 import download_from_r2
from app.services.prompt_lookup import get_prompt_for_feature
from app.services.modifier_assembly import gather_modifiers

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/exit-ticket", tags=["exit-ticket"])

MODEL = "claude-sonnet-4-6"
client = anthropic.Anthropic()


def _load_segment_content(topic_id, segment_number, supabase):
    """Load segment YAML or fall back to full learning asset."""
    topic_data = supabase.table("topics").select(
        "learning_asset_url, course_id"
    ).eq("id", topic_id).execute()

    if not topic_data.data:
        raise HTTPException(404, "Topic not found")

    learning_asset_url = topic_data.data[0].get("learning_asset_url")
    course_id = topic_data.data[0].get("course_id")

    # Try segment YAML first
    learning_asset = ""
    try:
        seg_asset = download_from_r2(
            f"{topic_id}/segments/segment_{segment_number}.yaml"
        ).decode("utf-8")
        learning_asset = seg_asset
    except Exception:
        pass

    # Fall back to full asset
    if not learning_asset and learning_asset_url:
        try:
            learning_asset = download_from_r2(learning_asset_url).decode("utf-8")
        except Exception:
            pass

    if not learning_asset:
        raise HTTPException(404, "No learning asset available")

    return learning_asset, course_id


def _build_system_prompt(base_prompt, modifier_text, learning_asset, segment_number):
    """Assemble system prompt from parts."""
    parts = [base_prompt]
    if modifier_text:
        parts.append(f"---\n\nMODIFIERS:\n\n{modifier_text}")
    parts.append(
        f"---\n\nLEARNING ASSET (Segment {segment_number}):\n\n{learning_asset}"
    )
    return "\n\n".join(parts)


@router.post("/{topic_id}/start")
async def start_exit_ticket(
    topic_id: str, request: Request, student: dict = Depends(get_current_student)
):
    body = await request.json()
    segment_number = body.get("segment_number")
    if not segment_number:
        raise HTTPException(400, "segment_number required")

    supabase = get_supabase()

    # Check for existing result
    existing = (
        supabase.table("exit_ticket_results")
        .select("*")
        .eq("topic_id", topic_id)
        .eq("segment_number", segment_number)
        .eq("student_id", student["id"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if existing.data:
        result = existing.data[0]
        if result["status"] in ("in_progress", "pass"):
            return {"result": result}

    # Load content
    learning_asset, course_id = _load_segment_content(
        topic_id, segment_number, supabase
    )

    framework_type = None
    if course_id:
        course_res = (
            supabase.table("courses")
            .select("framework_type")
            .eq("id", course_id)
            .execute()
        )
        if course_res.data:
            framework_type = course_res.data[0].get("framework_type")

    base_prompt = get_prompt_for_feature("exit_ticket", framework_type)
    modifier_text = gather_modifiers(
        feature="exit_ticket",
        student_id=student["id"],
        course_id=course_id,
        topic_id=topic_id,
    )

    system_prompt = _build_system_prompt(
        base_prompt, modifier_text, learning_asset, segment_number
    )

    # Generate tasks
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[
            {
                "role": "user",
                "content": (
                    "Generate exit ticket tasks for this segment. "
                    "Respond with a JSON array of task objects, each with a 'task' field "
                    'containing the task text. Example: [{"task": "..."}]. '
                    "Return ONLY the JSON array, no other text."
                ),
            }
        ],
    )

    tasks_text = response.content[0].text.strip()
    if tasks_text.startswith("```"):
        tasks_text = tasks_text[tasks_text.index("\n") + 1 :]
    if tasks_text.endswith("```"):
        tasks_text = tasks_text[:-3].strip()

    try:
        tasks = json.loads(tasks_text)
    except json.JSONDecodeError:
        tasks = [{"task": tasks_text}]

    # Store
    result_data = {
        "topic_id": topic_id,
        "segment_number": segment_number,
        "student_id": student["id"],
        "tasks": tasks,
        "responses": None,
        "evaluation": None,
        "status": "in_progress",
    }

    insert_result = (
        supabase.table("exit_ticket_results").insert(result_data).execute()
    )

    return {"result": insert_result.data[0]}


@router.post("/{topic_id}/submit")
async def submit_exit_ticket(
    topic_id: str, request: Request, student: dict = Depends(get_current_student)
):
    body = await request.json()
    segment_number = body.get("segment_number")
    responses = body.get("responses", [])

    if not segment_number:
        raise HTTPException(400, "segment_number required")
    if not responses:
        raise HTTPException(400, "responses required")

    supabase = get_supabase()

    # Load existing in-progress result
    existing = (
        supabase.table("exit_ticket_results")
        .select("*")
        .eq("topic_id", topic_id)
        .eq("segment_number", segment_number)
        .eq("student_id", student["id"])
        .eq("status", "in_progress")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not existing.data:
        raise HTTPException(404, "No in-progress exit ticket found")

    result = existing.data[0]
    tasks = result["tasks"]

    # Load content for evaluation
    learning_asset, course_id = _load_segment_content(
        topic_id, segment_number, supabase
    )

    framework_type = None
    if course_id:
        course_res = (
            supabase.table("courses")
            .select("framework_type")
            .eq("id", course_id)
            .execute()
        )
        if course_res.data:
            framework_type = course_res.data[0].get("framework_type")

    base_prompt = get_prompt_for_feature("exit_ticket", framework_type)
    modifier_text = gather_modifiers(
        feature="exit_ticket",
        student_id=student["id"],
        course_id=course_id,
        topic_id=topic_id,
    )

    system_prompt = _build_system_prompt(
        base_prompt, modifier_text, learning_asset, segment_number
    )

    # Build evaluation request
    eval_content = "TASKS AND STUDENT RESPONSES:\n\n"
    for i, task in enumerate(tasks):
        task_text = task.get("task", task) if isinstance(task, dict) else task
        response_text = responses[i] if i < len(responses) else "(no response)"
        eval_content += f"TASK {i + 1}: {task_text}\nSTUDENT RESPONSE {i + 1}: {response_text}\n\n"

    eval_content += (
        "Evaluate the student's responses against the learning asset's capabilities and success markers. "
        "Respond with a JSON object:\n"
        "{\n"
        '  "status": "pass" or "incomplete",\n'
        '  "demonstrated": "What the student demonstrated — specific evidence from their responses",\n'
        '  "not_there_yet": "What capabilities were not demonstrated — what\'s absent. Empty string if pass.",\n'
        '  "office_hours_prompt": "A ready-made prompt written AS the student — \'I understand X, but I\'m not sure about Y — can we talk through Z?\' Empty string if pass."\n'
        "}\n"
        "Return ONLY the JSON object, no other text."
    )

    eval_response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": eval_content}],
    )

    eval_text = eval_response.content[0].text.strip()
    if eval_text.startswith("```"):
        eval_text = eval_text[eval_text.index("\n") + 1 :]
    if eval_text.endswith("```"):
        eval_text = eval_text[:-3].strip()

    try:
        evaluation = json.loads(eval_text)
    except json.JSONDecodeError:
        evaluation = {
            "status": "incomplete",
            "demonstrated": "",
            "not_there_yet": eval_text,
            "office_hours_prompt": "",
        }

    status = evaluation.get("status", "incomplete")

    # Update result
    supabase.table("exit_ticket_results").update(
        {
            "responses": responses,
            "evaluation": evaluation,
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", result["id"]).execute()

    result["responses"] = responses
    result["evaluation"] = evaluation
    result["status"] = status

    return {"result": result}


@router.get("/{topic_id}/status")
async def get_exit_ticket_status(
    topic_id: str, request: Request, student: dict = Depends(get_current_student)
):
    segment_number = request.query_params.get("segment_number")
    if not segment_number:
        raise HTTPException(400, "segment_number query param required")

    supabase = get_supabase()

    existing = (
        supabase.table("exit_ticket_results")
        .select("*")
        .eq("topic_id", topic_id)
        .eq("segment_number", int(segment_number))
        .eq("student_id", student["id"])
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not existing.data:
        return {"status": "not_started", "result": None}

    return {"status": existing.data[0]["status"], "result": existing.data[0]}


@router.get("/{topic_id}/status/all")
async def get_all_exit_ticket_statuses(
    topic_id: str, request: Request, student: dict = Depends(get_current_student)
):
    """Return exit ticket status for every segment the student has attempted."""
    supabase = get_supabase()

    results = (
        supabase.table("exit_ticket_results")
        .select("segment_number, status, created_at")
        .eq("topic_id", topic_id)
        .eq("student_id", student["id"])
        .order("created_at", desc=True)
        .execute()
    )

    # Deduplicate — keep latest status per segment
    status_map = {}
    for r in results.data:
        seg = r["segment_number"]
        if seg not in status_map:
            status_map[seg] = r["status"]

    return {"statuses": status_map}
