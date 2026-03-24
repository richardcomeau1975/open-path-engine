# FILE: app/routers/test_env.py
"""
Admin Test Environment — manual override + selective re-generation.
Lets Richard create blank topics, upload hand-written outputs, and
re-run individual pipeline steps from any point.
"""

import logging
import json
from typing import Optional
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException

from app.services.supabase import get_supabase
from app.services.r2 import upload_text_to_r2, upload_bytes_to_r2, download_from_r2, generate_presigned_url

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/test", tags=["test-environment"])

# ── Auth helper (same pattern as admin.py) ──────────────────────

from app.routers.admin import _admin_tokens  # reuse the same token set

def require_admin(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    if token not in _admin_tokens:
        raise HTTPException(status_code=401, detail="Admin auth required")


# ── Output type → column + R2 key mapping ───────────────────────

OUTPUT_TYPES = {
    "learning_asset": {
        "column": "learning_asset_url",
        "r2_key": lambda tid: f"{tid}/learning_asset.md",
        "content_type": "text/markdown",
    },
    "podcast_script": {
        "column": "podcast_script_url",
        "r2_key": lambda tid: f"{tid}/podcast_script.md",
        "content_type": "text/markdown",
    },
    "podcast_audio": {
        "column": "podcast_audio_url",
        "r2_key": lambda tid: f"{tid}/podcast_audio.wav",
        "content_type": "audio/wav",
    },
    "notechart": {
        "column": "notechart_url",
        "r2_key": lambda tid: f"{tid}/notechart.json",
        "content_type": "application/json",
    },
    "visual_overview_script": {
        "column": "visual_overview_script_url",
        "r2_key": lambda tid: f"{tid}/visual_overview_script.json",
        "content_type": "application/json",
    },
    "visual_overview_images": {
        "column": "visual_overview_images",
        "r2_key": lambda tid: f"{tid}/images/",  # multiple files
        "content_type": "image/png",
    },
    "narration_audio": {
        "column": "visual_overview_audio_urls",
        "r2_key": lambda tid: f"{tid}/narration/",  # multiple files
        "content_type": "audio/wav",
    },
}

# Pipeline dependency chain
PIPELINE_STEPS = [
    "generate_learning_asset",
    "generate_podcast_script",
    "generate_notechart",
    "generate_visual_overview_script",
    "generate_images",
    "generate_podcast_audio",
    "generate_narration_audio",
]

# Which generator steps are downstream of the learning asset
DOWNSTREAM_OF_ASSET = [
    "generate_podcast_script",
    "generate_notechart",
    "generate_visual_overview_script",
    "generate_images",
    "generate_podcast_audio",
    "generate_narration_audio",
]


# ── 1. Create a blank test topic ────────────────────────────────

@router.post("/topics")
async def create_test_topic(
    request: Request,
    name: str = Form(...),
    course_id: str = Form(...),
):
    """Create a blank topic with no uploads and no generation. Just an empty shell."""
    require_admin(request)
    supabase = get_supabase()

    # Verify the course exists
    course = supabase.table("courses").select("id, student_id, name").eq("id", course_id).execute()
    if not course.data:
        raise HTTPException(status_code=404, detail="Course not found")

    # Create the topic
    result = supabase.table("topics").insert({
        "course_id": course_id,
        "name": name,
        "week_number": None,
        "generation_status": "idle",
    }).execute()

    topic = result.data[0]
    logger.info(f"Test env — created blank topic '{name}' ({topic['id']}) in course {course_id}")
    return {"topic": topic}


# ── 2. Get all outputs for a topic ──────────────────────────────

@router.get("/topics/{topic_id}/outputs")
async def get_topic_outputs(request: Request, topic_id: str):
    """Return status of all pipeline outputs for a topic."""
    require_admin(request)
    supabase = get_supabase()

    topic = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    t = topic.data[0]

    # Get course info for context
    course = supabase.table("courses").select("id, name, student_id, framework_type").eq("id", t["course_id"]).execute()
    course_data = course.data[0] if course.data else {}

    student = None
    if course_data.get("student_id"):
        s = supabase.table("students").select("id, name").eq("id", course_data["student_id"]).execute()
        student = s.data[0] if s.data else None

    outputs = {}
    for output_type, config in OUTPUT_TYPES.items():
        col = config["column"]
        value = t.get(col)

        if output_type in ("visual_overview_images", "narration_audio"):
            # This is a JSONB array of R2 keys
            exists = bool(value and isinstance(value, list) and len(value) > 0)
            file_count = len(value) if exists else 0
        else:
            exists = bool(value)
            file_count = 1 if exists else 0

        outputs[output_type] = {
            "exists": exists,
            "file_count": file_count,
            "r2_value": value,
        }

    return {
        "topic": {
            "id": t["id"],
            "name": t["name"],
            "course_id": t["course_id"],
            "generation_status": t.get("generation_status"),
        },
        "course": {
            "id": course_data.get("id"),
            "name": course_data.get("name"),
            "framework_type": course_data.get("framework_type"),
        },
        "student": student,
        "outputs": outputs,
    }


# ── 3. Replace any single output ────────────────────────────────

@router.put("/topics/{topic_id}/outputs/{output_type}")
async def replace_output(
    request: Request,
    topic_id: str,
    output_type: str,
    files: list[UploadFile] = File(...),
):
    """Upload a file to replace a pipeline output. For images/narration, accepts multiple files."""
    require_admin(request)

    if output_type not in OUTPUT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown output type: {output_type}. Valid: {list(OUTPUT_TYPES.keys())}")

    supabase = get_supabase()
    topic = supabase.table("topics").select("id").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    config = OUTPUT_TYPES[output_type]
    col = config["column"]

    if output_type == "visual_overview_images":
        # Multiple image files
        keys = []
        for i, f in enumerate(files):
            file_bytes = await f.read()
            key = f"{topic_id}/images/slide_{i + 1}.png"
            upload_bytes_to_r2(key, file_bytes, content_type="image/png")
            keys.append(key)
            logger.info(f"Test env — uploaded image {key} ({len(file_bytes)} bytes)")

        supabase.table("topics").update({col: keys}).eq("id", topic_id).execute()
        return {"replaced": output_type, "files": len(keys), "keys": keys}

    elif output_type == "narration_audio":
        # Multiple audio files
        keys = []
        for i, f in enumerate(files):
            file_bytes = await f.read()
            key = f"{topic_id}/narration/slide_{i + 1}.wav"
            upload_bytes_to_r2(key, file_bytes, content_type="audio/wav")
            keys.append(key)
            logger.info(f"Test env — uploaded narration {key} ({len(file_bytes)} bytes)")

        supabase.table("topics").update({col: keys}).eq("id", topic_id).execute()
        return {"replaced": output_type, "files": len(keys), "keys": keys}

    else:
        # Single file
        f = files[0]
        file_bytes = await f.read()
        key = config["r2_key"](topic_id)

        # For text files, use upload_text_to_r2 (expects string)
        if config["content_type"] in ("text/markdown", "application/json"):
            text = file_bytes.decode("utf-8")
            upload_text_to_r2(key, text)
        else:
            upload_bytes_to_r2(key, file_bytes, content_type=config["content_type"])

        supabase.table("topics").update({col: key}).eq("id", topic_id).execute()
        logger.info(f"Test env — replaced {output_type} for {topic_id} ({len(file_bytes)} bytes)")
        return {"replaced": output_type, "key": key}


# ── 4. Generate a single pipeline step ──────────────────────────

@router.post("/topics/{topic_id}/generate/{step}")
async def generate_single_step(request: Request, topic_id: str, step: str):
    """
    Run a single generation step using whatever upstream input currently exists.
    Does NOT run the full pipeline — just this one step.
    """
    require_admin(request)

    if step not in PIPELINE_STEPS:
        raise HTTPException(status_code=400, detail=f"Unknown step: {step}. Valid: {PIPELINE_STEPS}")

    supabase = get_supabase()

    # Load topic + course info (generators need framework_type)
    topic = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")
    t = topic.data[0]

    course = supabase.table("courses").select("id, student_id, framework_type").eq("id", t["course_id"]).execute()
    course_data = course.data[0] if course.data else {}
    framework_type = course_data.get("framework_type")
    student_id = course_data.get("student_id")
    course_id = course_data.get("id")

    try:
        await _run_step(step, topic_id, t, framework_type, student_id, course_id, supabase)
        return {"step": step, "status": "completed"}
    except Exception as e:
        logger.error(f"Test env — step {step} failed for {topic_id}: {e}")
        raise HTTPException(status_code=500, detail=f"Step {step} failed: {str(e)}")


# ── 5. Generate all downstream from learning asset ──────────────

@router.post("/topics/{topic_id}/generate-downstream")
async def generate_downstream(request: Request, topic_id: str):
    """
    Re-run all steps downstream of the learning asset.
    Uses whatever learning asset currently exists (pipeline-generated or manually replaced).
    """
    require_admin(request)
    supabase = get_supabase()

    topic = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")
    t = topic.data[0]

    # Verify learning asset exists
    if not t.get("learning_asset_url"):
        raise HTTPException(status_code=400, detail="No learning asset exists. Upload one first.")

    course = supabase.table("courses").select("id, student_id, framework_type").eq("id", t["course_id"]).execute()
    course_data = course.data[0] if course.data else {}
    framework_type = course_data.get("framework_type")
    student_id = course_data.get("student_id")
    course_id = course_data.get("id")

    results = {}
    for step in DOWNSTREAM_OF_ASSET:
        try:
            await _run_step(step, topic_id, t, framework_type, student_id, course_id, supabase)
            results[step] = "completed"
            logger.info(f"Test env — downstream step {step} completed for {topic_id}")
        except Exception as e:
            results[step] = f"failed: {str(e)}"
            logger.error(f"Test env — downstream step {step} failed for {topic_id}: {e}")
            # Continue with remaining steps — don't abort on failure

    return {"results": results}


# ── 6. Get presigned download URL for any output ────────────────

@router.get("/topics/{topic_id}/download/{output_type}")
async def download_output(request: Request, topic_id: str, output_type: str):
    """Get a presigned URL to download/view an output."""
    require_admin(request)

    if output_type not in OUTPUT_TYPES:
        raise HTTPException(status_code=400, detail=f"Unknown output type: {output_type}")

    supabase = get_supabase()
    topic = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    t = topic.data[0]
    col = OUTPUT_TYPES[output_type]["column"]
    value = t.get(col)

    if not value:
        raise HTTPException(status_code=404, detail=f"{output_type} not generated yet")

    if isinstance(value, list):
        # Multiple files — return presigned URLs for all
        urls = []
        for key in value:
            url = generate_presigned_url(key)
            urls.append({"key": key, "url": url})
        return {"output_type": output_type, "files": urls}
    else:
        url = generate_presigned_url(value)
        return {"output_type": output_type, "url": url, "key": value}


# ── 7. List all courses (for the topic creation dropdown) ───────

@router.get("/courses")
async def list_courses_for_test(request: Request):
    """List all courses with student info for the create-topic dropdown."""
    require_admin(request)
    supabase = get_supabase()

    courses = supabase.table("courses").select(
        "id, name, framework_type, student_id"
    ).is_("archived_at", "null").execute()

    # Attach student names
    student_ids = list(set(c["student_id"] for c in courses.data if c.get("student_id")))
    students = {}
    if student_ids:
        for sid in student_ids:
            s = supabase.table("students").select("id, name").eq("id", sid).execute()
            if s.data:
                students[sid] = s.data[0]["name"]

    result = []
    for c in courses.data:
        result.append({
            "id": c["id"],
            "name": c["name"],
            "framework_type": c.get("framework_type"),
            "student_name": students.get(c.get("student_id"), "Unknown"),
        })

    return {"courses": result}


# ── Internal: run a single pipeline step ────────────────────────

async def _run_step(step: str, topic_id: str, topic_row: dict, framework_type: str, student_id: str, course_id: str, supabase):
    """Run one generation step. Uses build_prompt + Anthropic batch/direct call + store_result pattern."""
    import asyncio
    from app.services.r2 import download_from_r2
    from app.services.batch_api import run_anthropic_batch

    gen_kwargs = dict(framework_type=framework_type, student_id=student_id, course_id=course_id)

    if step == "generate_learning_asset":
        from app.services.generators.learning_asset import build_learning_asset_prompt, store_learning_asset_result
        prompt = await build_learning_asset_prompt(topic_id, supabase, **gen_kwargs)
        results = run_anthropic_batch([{
            "custom_id": "learning_asset",
            "model": "claude-opus-4-20250514",
            "max_tokens": 16384,
            "prompt": prompt,
        }])
        text = results.get("learning_asset")
        if not text:
            raise Exception("Learning asset batch request failed")
        await store_learning_asset_result(topic_id, supabase, text)

    elif step == "generate_podcast_script":
        from app.services.generators.podcast_script import build_podcast_script_prompt, store_podcast_script_result
        la_text = download_from_r2(f"{topic_id}/learning_asset.md").decode("utf-8")
        prompt = await build_podcast_script_prompt(topic_id, supabase, la_text, **gen_kwargs)
        results = run_anthropic_batch([{
            "custom_id": "podcast_script",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 16384,
            "prompt": prompt,
        }])
        text = results.get("podcast_script")
        if not text:
            raise Exception("Podcast script batch request failed")
        await store_podcast_script_result(topic_id, supabase, text)

    elif step == "generate_notechart":
        from app.services.generators.notechart import build_notechart_prompt, store_notechart_result
        la_text = download_from_r2(f"{topic_id}/learning_asset.md").decode("utf-8")
        prompt = await build_notechart_prompt(topic_id, supabase, la_text, **gen_kwargs)
        results = run_anthropic_batch([{
            "custom_id": "notechart",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8000,
            "prompt": prompt,
        }])
        text = results.get("notechart")
        if not text:
            raise Exception("Notechart batch request failed")
        await store_notechart_result(topic_id, supabase, text)

    elif step == "generate_visual_overview_script":
        from app.services.generators.visual_overview import build_visual_overview_prompt, store_visual_overview_result
        la_text = download_from_r2(f"{topic_id}/learning_asset.md").decode("utf-8")
        prompt = await build_visual_overview_prompt(topic_id, supabase, la_text, **gen_kwargs)
        results = run_anthropic_batch([{
            "custom_id": "visual_overview",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8000,
            "prompt": prompt,
        }])
        text = results.get("visual_overview")
        if not text:
            raise Exception("Visual overview batch request failed")
        await store_visual_overview_result(topic_id, supabase, text)

    elif step == "generate_images":
        from app.services.generators.images import generate_images
        await generate_images(topic_id, supabase)

    elif step == "generate_podcast_audio":
        from app.services.generators.podcast_audio import generate_podcast_audio
        await generate_podcast_audio(topic_id, supabase)

    elif step == "generate_narration_audio":
        from app.services.generators.narration_audio import generate_narration_audio
        await generate_narration_audio(topic_id, supabase)

    else:
        raise ValueError(f"Unknown step: {step}")
