# FILE: app/routers/test_env.py
# Drop into open-path-engine/app/routers/

"""
Admin Test Environment — manual override + selective single-step generation.
Create blank topics, upload hand-written outputs, generate any single step.
"""

import logging
from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException

from app.services.supabase import get_supabase
from app.services.r2 import upload_text_to_r2, upload_bytes_to_r2, generate_presigned_url
# ^^^ VERIFY: confirm these three functions exist in r2.py with these exact names.
# If generate_presigned_url is named differently (e.g. presign_url, get_presigned_url),
# update this import to match.

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/admin/test", tags=["test-environment"])

# ── Auth (reuses admin token set) ────────────────────────────────

from app.routers.admin import _admin_tokens
# ^^^ VERIFY: confirm _admin_tokens is a module-level set in admin.py

def require_admin(request: Request):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "") if auth.startswith("Bearer ") else ""
    if token not in _admin_tokens:
        raise HTTPException(status_code=401, detail="Admin auth required")


# ── Output type → column + R2 key mapping ────────────────────────
# VERIFY: Run this in Supabase SQL Editor to confirm column names:
#   SELECT column_name FROM information_schema.columns
#   WHERE table_name = 'topics' ORDER BY column_name;
# If any column below doesn't match, fix it here.

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
        "r2_key": lambda tid: f"{tid}/images/",
        "content_type": "image/png",
    },
    "narration_audio": {
        "column": "narration_audio_urls",
        "r2_key": lambda tid: f"{tid}/narration/",
        "content_type": "audio/wav",
    },
}

# Valid generation step names (matches what the frontend sends)
GENERATE_STEPS = [
    "generate_learning_asset",
    "generate_podcast_script",
    "generate_notechart",
    "generate_visual_overview_script",
    "generate_images",
    "generate_podcast_audio",
    "generate_narration_audio",
]


# ══════════════════════════════════════════════════════════════════
# ENDPOINT 1: Create a blank test topic
# ══════════════════════════════════════════════════════════════════

@router.post("/topics")
async def create_test_topic(
    request: Request,
    name: str = Form(...),
    course_id: str = Form(...),
):
    """Create a blank topic — no uploads, no generation, just an empty shell."""
    require_admin(request)
    supabase = get_supabase()

    course = supabase.table("courses").select("id, student_id, name").eq("id", course_id).execute()
    if not course.data:
        raise HTTPException(status_code=404, detail="Course not found")

    result = supabase.table("topics").insert({
        "course_id": course_id,
        "name": name,
        "week_number": None,
        "generation_status": "idle",
    }).execute()

    topic = result.data[0]
    logger.info(f"Test env — created blank topic '{name}' ({topic['id']})")
    return {"topic": topic}


# ══════════════════════════════════════════════════════════════════
# ENDPOINT 2: Get all outputs for a topic
# ══════════════════════════════════════════════════════════════════

@router.get("/topics/{topic_id}/outputs")
async def get_topic_outputs(request: Request, topic_id: str):
    """Return status of every pipeline output slot for a topic."""
    require_admin(request)
    supabase = get_supabase()

    topic = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")
    t = topic.data[0]

    course = supabase.table("courses").select(
        "id, name, student_id, framework_type"
    ).eq("id", t["course_id"]).execute()
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


# ══════════════════════════════════════════════════════════════════
# ENDPOINT 3: Replace any single output
# ══════════════════════════════════════════════════════════════════

@router.put("/topics/{topic_id}/outputs/{output_type}")
async def replace_output(
    request: Request,
    topic_id: str,
    output_type: str,
    files: list[UploadFile] = File(...),
):
    """Upload file(s) to replace a pipeline output."""
    require_admin(request)

    if output_type not in OUTPUT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown output type: {output_type}. Valid: {list(OUTPUT_TYPES.keys())}",
        )

    supabase = get_supabase()
    topic = supabase.table("topics").select("id").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    config = OUTPUT_TYPES[output_type]
    col = config["column"]

    # ── Multi-file outputs (images, narration) ──────────────────

    if output_type == "visual_overview_images":
        keys = []
        for i, f in enumerate(files):
            file_bytes = await f.read()
            key = f"{topic_id}/images/slide_{i + 1}.png"
            upload_bytes_to_r2(key, file_bytes, content_type="image/png")
            keys.append(key)
        supabase.table("topics").update({col: keys}).eq("id", topic_id).execute()
        logger.info(f"Test env — uploaded {len(keys)} images for {topic_id}")
        return {"replaced": output_type, "files": len(keys), "keys": keys}

    elif output_type == "narration_audio":
        keys = []
        for i, f in enumerate(files):
            file_bytes = await f.read()
            key = f"{topic_id}/narration/slide_{i + 1}.wav"
            upload_bytes_to_r2(key, file_bytes, content_type="audio/wav")
            keys.append(key)
        supabase.table("topics").update({col: keys}).eq("id", topic_id).execute()
        logger.info(f"Test env — uploaded {len(keys)} narration files for {topic_id}")
        return {"replaced": output_type, "files": len(keys), "keys": keys}

    # ── Single-file outputs ─────────────────────────────────────

    else:
        f = files[0]
        file_bytes = await f.read()
        key = config["r2_key"](topic_id)

        if config["content_type"] in ("text/markdown", "application/json"):
            text = file_bytes.decode("utf-8")
            upload_text_to_r2(key, text)
        else:
            upload_bytes_to_r2(key, file_bytes, content_type=config["content_type"])

        supabase.table("topics").update({col: key}).eq("id", topic_id).execute()
        logger.info(f"Test env — replaced {output_type} for {topic_id} ({len(file_bytes)} bytes)")
        return {"replaced": output_type, "key": key}


# ══════════════════════════════════════════════════════════════════
# ENDPOINT 4: Generate a single pipeline step
# ══════════════════════════════════════════════════════════════════

@router.post("/topics/{topic_id}/generate/{step}")
async def generate_single_step(request: Request, topic_id: str, step: str):
    """
    Run ONE generation step using whatever upstream input currently exists on R2.
    Does not touch any other outputs.
    """
    require_admin(request)

    if step not in GENERATE_STEPS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown step: {step}. Valid: {GENERATE_STEPS}",
        )

    supabase = get_supabase()

    topic = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")
    t = topic.data[0]

    course = supabase.table("courses").select(
        "id, student_id, framework_type"
    ).eq("id", t["course_id"]).execute()
    course_data = course.data[0] if course.data else {}
    framework_type = course_data.get("framework_type")
    student_id = course_data.get("student_id")
    course_id = course_data.get("id")

    try:
        await _run_single_step(
            step=step,
            topic_id=topic_id,
            framework_type=framework_type,
            student_id=student_id,
            course_id=course_id,
            supabase_client=supabase,
        )
        return {"step": step, "status": "completed"}
    except Exception as e:
        logger.error(f"Test env — {step} failed for {topic_id}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"{step} failed: {str(e)}")


# ══════════════════════════════════════════════════════════════════
# ENDPOINT 4b: Generate downstream from any starting point
# ══════════════════════════════════════════════════════════════════

DOWNSTREAM_MAP = {
    "learning_asset": [
        "generate_podcast_script",
        "generate_notechart",
        "generate_visual_overview_script",
        "generate_images",
        "generate_podcast_audio",
        "generate_narration_audio",
    ],
    "podcast_script": [
        "generate_podcast_audio",
    ],
    "visual_overview_script": [
        "generate_images",
        "generate_narration_audio",
    ],
    "podcast_audio": [],
    "notechart": [],
    "visual_overview_images": [],
    "narration_audio": [],
}

@router.post("/topics/{topic_id}/generate-from/{output_type}")
async def generate_from(request: Request, topic_id: str, output_type: str):
    """
    Generate everything downstream from a given output type.
    Runs each step sequentially. Not batched — this is for testing, not production.
    """
    require_admin(request)

    if output_type not in DOWNSTREAM_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown output type: {output_type}. Valid: {list(DOWNSTREAM_MAP.keys())}",
        )

    steps = DOWNSTREAM_MAP[output_type]
    if not steps:
        raise HTTPException(status_code=400, detail=f"{output_type} has no downstream outputs")

    supabase = get_supabase()

    topic = supabase.table("topics").select("*").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")
    t = topic.data[0]

    course = supabase.table("courses").select(
        "id, student_id, framework_type"
    ).eq("id", t["course_id"]).execute()
    course_data = course.data[0] if course.data else {}
    framework_type = course_data.get("framework_type")
    student_id = course_data.get("student_id")
    course_id = course_data.get("id")

    results = {}
    for step in steps:
        try:
            await _run_single_step(
                step=step,
                topic_id=topic_id,
                framework_type=framework_type,
                student_id=student_id,
                course_id=course_id,
                supabase_client=supabase,
            )
            results[step] = "completed"
            logger.info(f"Test env — downstream {step} completed for {topic_id}")
        except Exception as e:
            results[step] = f"failed: {str(e)}"
            logger.error(f"Test env — downstream {step} failed for {topic_id}: {e}", exc_info=True)

    return {"from": output_type, "results": results}


# ══════════════════════════════════════════════════════════════════
# ENDPOINT 5: Download/view any output
# ══════════════════════════════════════════════════════════════════

@router.get("/topics/{topic_id}/download/{output_type}")
async def download_output(request: Request, topic_id: str, output_type: str):
    """Get presigned URL(s) to view/download an output."""
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
        urls = [{"key": key, "url": generate_presigned_url(key)} for key in value]
        return {"output_type": output_type, "files": urls}
    else:
        return {"output_type": output_type, "url": generate_presigned_url(value), "key": value}


# ══════════════════════════════════════════════════════════════════
# ENDPOINT 6: List courses (for topic creation dropdown)
# ══════════════════════════════════════════════════════════════════

@router.get("/courses")
async def list_courses_for_test(request: Request):
    """List all courses with student names for the course picker."""
    require_admin(request)
    supabase = get_supabase()

    courses = supabase.table("courses").select(
        "id, name, framework_type, student_id"
    ).is_("archived_at", "null").execute()

    student_ids = list(set(c["student_id"] for c in courses.data if c.get("student_id")))
    students = {}
    for sid in student_ids:
        s = supabase.table("students").select("id, name").eq("id", sid).execute()
        if s.data:
            students[sid] = s.data[0]["name"]

    return {
        "courses": [
            {
                "id": c["id"],
                "name": c["name"],
                "framework_type": c.get("framework_type"),
                "student_name": students.get(c.get("student_id"), "Unknown"),
            }
            for c in courses.data
        ]
    }


# ══════════════════════════════════════════════════════════════════
# INTERNAL: Run one generation step
# ══════════════════════════════════════════════════════════════════
#
# IMPORTANT — VERIFY EVERY FUNCTION SIGNATURE BELOW:
#
# Before deploying, open each generator file and confirm the function
# signature matches what's called here. If a generator takes different
# params (e.g. supabase_client instead of supabase, or doesn't accept
# student_id), fix the call below to match THE ACTUAL CODE.
#
# Check these files:
#   app/services/generators/learning_asset.py  → generate_learning_asset(?)
#   app/services/generators/podcast_script.py  → generate_podcast_script(?)
#   app/services/generators/notechart.py       → generate_notechart(?)
#   app/services/generators/visual_overview.py → generate_visual_overview(?)
#   app/services/generators/images.py          → generate_images(?)
#   app/services/generators/podcast_audio.py   → generate_podcast_audio(?)
#   app/services/generators/narration_audio.py → generate_narration_audio(?)
#

async def _run_single_step(
    step: str,
    topic_id: str,
    framework_type: str,
    student_id: str,
    course_id: str,
    supabase_client,
):
    """
    Call one generator function. Each generator's signature must be verified
    against the actual code — see comments above.
    """

    # Text generators use build_prompt + batch API + store_result pattern
    # (no standalone generate_* functions for these)
    gen_kwargs = dict(framework_type=framework_type, student_id=student_id, course_id=course_id)

    if step == "generate_learning_asset":
        from app.services.generators.learning_asset import build_learning_asset_prompt, store_learning_asset_result
        from app.services.batch_api import run_anthropic_batch
        prompt = await build_learning_asset_prompt(topic_id, supabase_client, **gen_kwargs)
        results = run_anthropic_batch([{
            "custom_id": "learning_asset",
            "model": "claude-opus-4-20250514",
            "max_tokens": 16384,
            "prompt": prompt,
        }])
        text = results.get("learning_asset")
        if not text:
            raise Exception("Learning asset batch request failed")
        await store_learning_asset_result(topic_id, supabase_client, text)

    elif step == "generate_podcast_script":
        from app.services.generators.podcast_script import build_podcast_script_prompt, store_podcast_script_result
        from app.services.batch_api import run_anthropic_batch
        from app.services.r2 import download_from_r2
        la_text = download_from_r2(f"{topic_id}/learning_asset.md").decode("utf-8")
        prompt = await build_podcast_script_prompt(topic_id, supabase_client, la_text, **gen_kwargs)
        results = run_anthropic_batch([{
            "custom_id": "podcast_script",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 16384,
            "prompt": prompt,
        }])
        text = results.get("podcast_script")
        if not text:
            raise Exception("Podcast script batch request failed")
        await store_podcast_script_result(topic_id, supabase_client, text)

    elif step == "generate_notechart":
        from app.services.generators.notechart import build_notechart_prompt, store_notechart_result
        from app.services.batch_api import run_anthropic_batch
        from app.services.r2 import download_from_r2
        la_text = download_from_r2(f"{topic_id}/learning_asset.md").decode("utf-8")
        prompt = await build_notechart_prompt(topic_id, supabase_client, la_text, **gen_kwargs)
        results = run_anthropic_batch([{
            "custom_id": "notechart",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8000,
            "prompt": prompt,
        }])
        text = results.get("notechart")
        if not text:
            raise Exception("Notechart batch request failed")
        await store_notechart_result(topic_id, supabase_client, text)

    elif step == "generate_visual_overview_script":
        from app.services.generators.visual_overview import build_visual_overview_prompt, store_visual_overview_result
        from app.services.batch_api import run_anthropic_batch
        from app.services.r2 import download_from_r2
        la_text = download_from_r2(f"{topic_id}/learning_asset.md").decode("utf-8")
        prompt = await build_visual_overview_prompt(topic_id, supabase_client, la_text, **gen_kwargs)
        results = run_anthropic_batch([{
            "custom_id": "visual_overview",
            "model": "claude-sonnet-4-20250514",
            "max_tokens": 8000,
            "prompt": prompt,
        }])
        text = results.get("visual_overview")
        if not text:
            raise Exception("Visual overview batch request failed")
        await store_visual_overview_result(topic_id, supabase_client, text)

    elif step == "generate_images":
        from app.services.generators.images import generate_images
        # VERIFY signature: likely (topic_id, supabase_client)
        await generate_images(topic_id, supabase_client)

    elif step == "generate_podcast_audio":
        from app.services.generators.podcast_audio import generate_podcast_audio
        # VERIFY signature: likely (topic_id, supabase_client)
        await generate_podcast_audio(topic_id, supabase_client)

    elif step == "generate_narration_audio":
        from app.services.generators.narration_audio import generate_narration_audio
        # VERIFY signature: likely (topic_id, supabase_client)
        await generate_narration_audio(topic_id, supabase_client)

    else:
        raise ValueError(f"Unknown step: {step}")
