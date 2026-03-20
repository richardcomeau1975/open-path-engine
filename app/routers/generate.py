"""
Generation endpoint — kicks off the pipeline for a topic.
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException, Request

from app.services.pipeline import run_pipeline
from app.services.supabase import get_supabase

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/topics/{topic_id}/generate")
async def generate_topic(topic_id: str, request: Request):
    """
    Start the generation pipeline for a topic.
    Returns immediately — pipeline runs in background.
    """
    supabase = get_supabase()

    # Verify topic exists
    topic_result = supabase.table("topics").select("id, parsed_text_url, generation_status").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = topic_result.data[0]

    # Must have parsed text
    if not topic.get("parsed_text_url"):
        raise HTTPException(status_code=400, detail="No parsed text found — upload files first")

    # Don't start if already generating
    if topic.get("generation_status") == "generating":
        raise HTTPException(status_code=409, detail="Generation already in progress")

    # Kick off pipeline in background
    asyncio.create_task(run_pipeline(topic_id, supabase))

    return {"status": "started", "topic_id": topic_id, "message": "Generation pipeline started"}
