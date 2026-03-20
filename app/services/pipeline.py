"""
Generation pipeline orchestrator.
Runs all generation steps for a topic in sequence.
Each step is a placeholder until real generators are wired in.
"""

import asyncio
import logging
import traceback
from datetime import datetime, timezone
from uuid import UUID

from app.services.generators.learning_asset import generate_learning_asset as gen_learning_asset
from app.services.generators.podcast_script import generate_podcast_script as gen_podcast_script
from app.services.generators.notechart import generate_notechart as gen_notechart
from app.services.generators.visual_overview import generate_visual_overview_script as gen_visual_overview
from app.services.generators.images import generate_images as gen_images
from app.services.generators.podcast_audio import generate_podcast_audio as gen_podcast_audio
from app.services.generators.narration_audio import generate_narration_audio as gen_narration_audio

logger = logging.getLogger(__name__)

# Step definitions — order matters
PIPELINE_STEPS = [
    "parse_files",
    "generate_learning_asset",
    "generate_podcast_script",
    "generate_notechart",
    "generate_visual_overview_script",
    "generate_images",
    "generate_podcast_audio",
    "generate_visual_overview_audio",
]


async def run_pipeline(topic_id: str, supabase_client):
    """
    Run the full generation pipeline for a topic.
    Creates a batch_job, runs each step, updates status throughout.
    """
    job_id = None
    try:
        # Create batch_job row
        job_result = supabase_client.table("batch_jobs").insert({
            "topic_id": topic_id,
            "status": "running",
            "current_step": PIPELINE_STEPS[0],
            "steps_completed": [],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        job_id = job_result.data[0]["id"]

        # Update topic status
        supabase_client.table("topics").update({
            "generation_status": "generating"
        }).eq("id", topic_id).execute()

        steps_completed = []

        for step_name in PIPELINE_STEPS:
            logger.info(f"Pipeline [{topic_id}] — running step: {step_name}")

            # Update batch_job current step
            supabase_client.table("batch_jobs").update({
                "current_step": step_name,
            }).eq("id", job_id).execute()

            # Run the step (skip parse_files — already done during upload)
            if step_name == "parse_files":
                logger.info(f"Pipeline [{topic_id}] — parse_files: already done during upload, skipping")
            elif step_name == "generate_learning_asset":
                await gen_learning_asset(topic_id, supabase_client)
            elif step_name == "generate_podcast_script":
                await gen_podcast_script(topic_id, supabase_client)
            elif step_name == "generate_notechart":
                await gen_notechart(topic_id, supabase_client)
            elif step_name == "generate_visual_overview_script":
                await gen_visual_overview(topic_id, supabase_client)
            elif step_name == "generate_images":
                await gen_images(topic_id, supabase_client)
            elif step_name == "generate_podcast_audio":
                await gen_podcast_audio(topic_id, supabase_client)
            elif step_name == "generate_visual_overview_audio":
                await gen_narration_audio(topic_id, supabase_client)
            else:
                logger.warning(f"Pipeline [{topic_id}] — unknown step: {step_name}")

            steps_completed.append(step_name)
            supabase_client.table("batch_jobs").update({
                "steps_completed": steps_completed,
            }).eq("id", job_id).execute()

        # Mark complete
        supabase_client.table("batch_jobs").update({
            "status": "completed",
            "current_step": None,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()

        supabase_client.table("topics").update({
            "generation_status": "completed"
        }).eq("id", topic_id).execute()

        logger.info(f"Pipeline [{topic_id}] — COMPLETED successfully")

    except Exception as e:
        logger.error(f"Pipeline [{topic_id}] — FAILED: {e}\n{traceback.format_exc()}")

        if job_id:
            supabase_client.table("batch_jobs").update({
                "status": "failed",
                "error_log": str(e),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", job_id).execute()

        supabase_client.table("topics").update({
            "generation_status": "failed"
        }).eq("id", topic_id).execute()
