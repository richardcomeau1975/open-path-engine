"""
Generation pipeline orchestrator.
Uses Anthropic Batch API for Opus/Sonnet calls (50% cost reduction).
Non-Anthropic calls (OpenAI images, Gemini TTS) run directly.
"""

import asyncio
import logging
import traceback
from datetime import datetime, timezone

from app.services.batch_api import run_anthropic_batch
from app.services.generators.learning_asset import (
    build_learning_asset_prompt, store_learning_asset_result, MODEL as LA_MODEL, MAX_TOKENS as LA_MAX_TOKENS,
)
from app.services.generators.podcast_script import (
    build_podcast_script_prompt, store_podcast_script_result, MODEL as PS_MODEL, MAX_TOKENS as PS_MAX_TOKENS,
)
from app.services.generators.visual_overview import (
    build_visual_overview_prompt, store_visual_overview_result, MODEL as VO_MODEL, MAX_TOKENS as VO_MAX_TOKENS,
)
from app.services.generators.podcast_audio import generate_podcast_audio as gen_podcast_audio
from app.services.generators.narration_audio import generate_narration_audio as gen_narration_audio
from app.services.r2 import download_from_r2

logger = logging.getLogger(__name__)

# Step definitions for tracking
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


def _update_step(supabase_client, job_id, step_name, steps_completed):
    """Update batch_job with current step and completed steps."""
    supabase_client.table("batch_jobs").update({
        "current_step": step_name,
        "steps_completed": steps_completed,
    }).eq("id", job_id).execute()


async def run_pipeline(topic_id: str, supabase_client):
    """
    Run the full generation pipeline for a topic.

    Flow:
    1. Build learning asset prompt → submit as Batch API (Opus)
    2. Store learning asset result on R2
    3. Build podcast + notechart + visual_overview prompts → submit as ONE Batch API (Sonnet)
    4. Store all three results on R2
    5. Run images (OpenAI), podcast_audio (Gemini), narration_audio (Gemini) concurrently
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

        # Get course and student info for framework lookup + modifier assembly
        topic_result = supabase_client.table("topics").select("course_id").eq("id", topic_id).execute()
        course_id = topic_result.data[0]["course_id"] if topic_result.data else None
        framework_type = None
        student_id = None
        if course_id:
            course_result = supabase_client.table("courses").select("id, student_id, framework_type").eq("id", course_id).execute()
            if course_result.data:
                framework_type = course_result.data[0].get("framework_type")
                student_id = course_result.data[0].get("student_id")
        logger.info(f"Pipeline [{topic_id}] — framework_type: {framework_type}, student_id: {student_id}, course_id: {course_id}")

        # Update topic status
        supabase_client.table("topics").update({
            "generation_status": "generating"
        }).eq("id", topic_id).execute()

        steps_completed = ["parse_files"]
        gen_kwargs = dict(framework_type=framework_type, student_id=student_id, course_id=course_id)

        # ── BATCH 1: Learning Asset (Opus) ──────────────────────
        _update_step(supabase_client, job_id, "generate_learning_asset", steps_completed)
        logger.info(f"Pipeline [{topic_id}] — building learning asset prompt")

        la_prompt = await build_learning_asset_prompt(topic_id, supabase_client, **gen_kwargs)

        logger.info(f"Pipeline [{topic_id}] — submitting learning asset to Batch API")
        la_results = await run_anthropic_batch([{
            "custom_id": "learning_asset",
            "model": LA_MODEL,
            "max_tokens": LA_MAX_TOKENS,
            "prompt": la_prompt,
        }])

        la_text = la_results.get("learning_asset")
        if not la_text:
            raise Exception("Learning asset batch request failed — cannot continue pipeline")

        await store_learning_asset_result(topic_id, supabase_client, la_text)
        steps_completed.append("generate_learning_asset")
        _update_step(supabase_client, job_id, "generate_learning_asset", steps_completed)
        logger.info(f"Pipeline [{topic_id}] — learning asset complete ({len(la_text)} chars)")

        # ── BATCH 2: Podcast + Notechart + Visual Overview (Sonnet) ─
        _update_step(supabase_client, job_id, "generate_podcast_script", steps_completed)
        logger.info(f"Pipeline [{topic_id}] — building Sonnet batch prompts")

        # Notechart questions extracted from YAML during learning asset storage — skip batch
        steps_completed.append("generate_notechart")

        # Podcast + Visual Overview read the learning asset
        learning_asset = la_text

        ps_prompt = await build_podcast_script_prompt(topic_id, supabase_client, learning_asset, **gen_kwargs)
        vo_prompt = await build_visual_overview_prompt(topic_id, supabase_client, learning_asset, **gen_kwargs)

        logger.info(f"Pipeline [{topic_id}] — submitting podcast + visual_overview to Batch API")
        sonnet_results = await run_anthropic_batch([
            {"custom_id": "podcast_script", "model": PS_MODEL, "max_tokens": PS_MAX_TOKENS, "prompt": ps_prompt},
            {"custom_id": "visual_overview", "model": VO_MODEL, "max_tokens": VO_MAX_TOKENS, "prompt": vo_prompt},
        ])

        # Store results — track which ones succeeded
        errors = []

        ps_text = sonnet_results.get("podcast_script")
        if ps_text:
            await store_podcast_script_result(topic_id, supabase_client, ps_text)
            steps_completed.append("generate_podcast_script")
            logger.info(f"Pipeline [{topic_id}] — podcast script complete ({len(ps_text)} chars)")
        else:
            errors.append("podcast_script batch request failed")
            logger.error(f"Pipeline [{topic_id}] — podcast script FAILED")

        vo_text = sonnet_results.get("visual_overview")
        if vo_text:
            await store_visual_overview_result(topic_id, supabase_client, vo_text)
            steps_completed.append("generate_visual_overview_script")
            logger.info(f"Pipeline [{topic_id}] — visual overview complete ({len(vo_text)} chars)")
        else:
            errors.append("visual_overview batch request failed")
            logger.error(f"Pipeline [{topic_id}] — visual overview FAILED")

        _update_step(supabase_client, job_id, "generate_visual_overview_script", steps_completed)

        # Images removed from pipeline — visual overview uses typography
        steps_completed.append("generate_images")

        # ── PHASE 3: Non-Anthropic calls (TTS only) ─────
        phase3_tasks = []

        # Podcast audio depends on podcast script
        if ps_text:
            phase3_tasks.append(("generate_podcast_audio", gen_podcast_audio(topic_id, supabase_client)))

        # Narration audio depends on visual overview script
        if vo_text:
            phase3_tasks.append(("generate_visual_overview_audio", gen_narration_audio(topic_id, supabase_client)))

        if phase3_tasks:
            _update_step(supabase_client, job_id, "generate_images", steps_completed)
            logger.info(f"Pipeline [{topic_id}] — running {len(phase3_tasks)} non-Anthropic task(s) concurrently")

            results = await asyncio.gather(
                *[task for _, task in phase3_tasks],
                return_exceptions=True,
            )

            for (step_name, _), result in zip(phase3_tasks, results):
                if isinstance(result, Exception):
                    errors.append(f"{step_name}: {str(result)}")
                    logger.error(f"Pipeline [{topic_id}] — {step_name} FAILED: {result}")
                else:
                    steps_completed.append(step_name)
                    logger.info(f"Pipeline [{topic_id}] — {step_name} complete")

        _update_step(supabase_client, job_id, None, steps_completed)

        # ── Final status ────────────────────────────────────────
        if errors:
            supabase_client.table("batch_jobs").update({
                "status": "failed",
                "current_step": None,
                "error_log": "; ".join(errors),
                "completed_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", job_id).execute()

            supabase_client.table("topics").update({
                "generation_status": "failed"
            }).eq("id", topic_id).execute()

            logger.error(f"Pipeline [{topic_id}] — COMPLETED WITH ERRORS: {errors}")
        else:
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
