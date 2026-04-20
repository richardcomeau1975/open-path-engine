"""
Generation endpoint — kicks off the pipeline for a topic.
"""

import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request

from app.middleware.clerk_auth import get_current_student
from app.services.pipeline import run_pipeline
from app.services.supabase import get_supabase

router = APIRouter()


@router.post("/api/topics/{topic_id}/generate")
async def generate_topic(topic_id: str, request: Request, student: dict = Depends(get_current_student)):
    """
    Start the generation pipeline for a topic.
    Returns immediately — pipeline runs in background.
    """
    supabase = get_supabase()

    # Verify topic exists and belongs to this student
    topic_result = supabase.table("topics").select("id, parsed_text_url, generation_status, courses(student_id)").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = topic_result.data[0]
    if topic.get("courses", {}).get("student_id") != student["id"]:
        raise HTTPException(status_code=403, detail="Not your topic")

    # Must have parsed text
    if not topic.get("parsed_text_url"):
        raise HTTPException(status_code=400, detail="No parsed text found — upload files first")

    # Don't start if already generating
    if topic.get("generation_status") == "generating":
        raise HTTPException(status_code=409, detail="Generation already in progress")

    # Kick off pipeline in background
    asyncio.create_task(run_pipeline(topic_id, supabase))

    return {"status": "started", "topic_id": topic_id, "message": "Generation pipeline started"}
