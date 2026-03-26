"""
Admin test-mode endpoints for topic management.
Allows admins to create topics, inspect output status, upload/delete outputs,
generate individual outputs (with real or test prompts), and trigger downstream generation.
"""

import asyncio
import uuid
import logging
from typing import Optional

import anthropic
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase
from app.services.r2 import (
    download_from_r2,
    upload_text_to_r2,
    upload_bytes_to_r2,
    generate_presigned_url,
)
from app.services.modifier_assembly import gather_modifiers

# Text generators
from app.services.generators.learning_asset import (
    build_learning_asset_prompt,
    store_learning_asset_result,
)
from app.services.generators.podcast_script import (
    build_podcast_script_prompt,
    store_podcast_script_result,
)
from app.services.generators.notechart import (
    build_notechart_prompt,
    store_notechart_result,
)
from app.services.generators.visual_overview import (
    build_visual_overview_prompt,
    store_visual_overview_result,
)

# Media generators
from app.services.generators.images import generate_images
from app.services.generators.podcast_audio import generate_podcast_audio
from app.services.generators.narration_audio import generate_narration_audio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["topic-admin"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TEXT_OUTPUT_TYPES = {
    "learning_asset",
    "podcast_script",
    "notechart",
    "visual_overview_script",
}

MEDIA_OUTPUT_TYPES = {
    "visual_overview_images",
    "podcast_audio",
    "narration_audio",
}

ALL_OUTPUT_TYPES = TEXT_OUTPUT_TYPES | MEDIA_OUTPUT_TYPES

# Maps output type -> DB column name
COLUMN_MAP = {
    "learning_asset": "learning_asset_url",
    "podcast_script": "podcast_script_url",
    "podcast_audio": "podcast_audio_url",
    "notechart": "notechart_url",
    "visual_overview_script": "visual_overview_script_url",
    "visual_overview_images": "visual_overview_images",
    "narration_audio": "visual_overview_audio_urls",
}

# Maps output type -> R2 key template (single-file types only)
R2_KEY_MAP = {
    "learning_asset": "{topic_id}/learning_asset.md",
    "podcast_script": "{topic_id}/podcast_script.md",
    "podcast_audio": "{topic_id}/podcast_audio.wav",
    "notechart": "{topic_id}/notechart.json",
    "visual_overview_script": "{topic_id}/visual_overview_script.json",
}

# Multi-file types use patterns:
#   visual_overview_images -> {topic_id}/images/slide_{N}.png
#   narration_audio        -> {topic_id}/narration/slide_{N}.wav

# Maps output type -> feature key for get_prompt_for_feature
FEATURE_KEY_MAP = {
    "learning_asset": "learning_asset_generator",
    "podcast_script": "podcast_generator",
    "notechart": "notechart",
    "visual_overview_script": "visual_overview",
}

# Content types for upload
CONTENT_TYPE_MAP = {
    "learning_asset": "text/markdown",
    "podcast_script": "text/markdown",
    "podcast_audio": "audio/wav",
    "notechart": "application/json",
    "visual_overview_script": "application/json",
    "visual_overview_images": "image/png",
    "narration_audio": "audio/wav",
}

# Columns that hold JSONB arrays (multi-file outputs)
ARRAY_COLUMNS = {"visual_overview_images", "narration_audio"}


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def require_admin_student(student: dict = Depends(get_current_student)):
    if not student.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return student


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class CreateTopicBody(BaseModel):
    name: str
    course_id: str


class GenerateTestBody(BaseModel):
    prompt: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_topic_or_404(sb, topic_id: str) -> dict:
    result = sb.table("topics").select("*").eq("id", topic_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Topic not found")
    return result.data[0]


def _validate_output_type(output_type: str) -> None:
    if output_type not in ALL_OUTPUT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid output type: {output_type}. Must be one of: {sorted(ALL_OUTPUT_TYPES)}",
        )


async def _read_upstream_text(topic_id: str, output_type: str, sb) -> str:
    """Read the upstream content from R2 that a generator needs as input."""
    topic = _get_topic_or_404(sb, topic_id)

    if output_type == "learning_asset":
        # learning_asset reads parsed_text
        parsed_url = topic.get("parsed_text_url")
        if not parsed_url:
            raise HTTPException(status_code=400, detail="No parsed text available for this topic")
        return download_from_r2(parsed_url).decode("utf-8")
    else:
        # All other text outputs read the learning_asset
        la_url = topic.get("learning_asset_url")
        if not la_url:
            raise HTTPException(status_code=400, detail="No learning asset available for this topic")
        return download_from_r2(la_url).decode("utf-8")


async def _call_claude(prompt: str, model: str, max_tokens: int) -> str:
    """Call Claude via asyncio.to_thread so it doesn't block the event loop."""
    client = anthropic.Anthropic()
    response = await asyncio.to_thread(
        client.messages.create,
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


async def _generate_text_output(
    topic_id: str,
    output_type: str,
    sb,
    student: dict,
) -> str:
    """Generate a single text output using the real system prompt."""
    topic = _get_topic_or_404(sb, topic_id)
    course_id = topic.get("course_id")
    student_id = student.get("id")

    if output_type == "learning_asset":
        prompt = await build_learning_asset_prompt(
            topic_id, sb,
            student_id=student_id,
            course_id=course_id,
        )
        result_text = await _call_claude(prompt, model="claude-opus-4-20250514", max_tokens=16384)
        await store_learning_asset_result(topic_id, sb, result_text)

    elif output_type == "podcast_script":
        upstream = await _read_upstream_text(topic_id, output_type, sb)
        prompt = await build_podcast_script_prompt(
            topic_id, sb, upstream,
            student_id=student_id,
            course_id=course_id,
        )
        result_text = await _call_claude(prompt, model="claude-sonnet-4-20250514", max_tokens=16384)
        await store_podcast_script_result(topic_id, sb, result_text)

    elif output_type == "notechart":
        upstream = await _read_upstream_text(topic_id, output_type, sb)
        prompt = await build_notechart_prompt(
            topic_id, sb, upstream,
            student_id=student_id,
            course_id=course_id,
        )
        result_text = await _call_claude(prompt, model="claude-sonnet-4-20250514", max_tokens=8192)
        await store_notechart_result(topic_id, sb, result_text)

    elif output_type == "visual_overview_script":
        upstream = await _read_upstream_text(topic_id, output_type, sb)
        prompt = await build_visual_overview_prompt(
            topic_id, sb, upstream,
            student_id=student_id,
            course_id=course_id,
        )
        result_text = await _call_claude(prompt, model="claude-sonnet-4-20250514", max_tokens=8192)
        await store_visual_overview_result(topic_id, sb, result_text)

    else:
        raise HTTPException(status_code=400, detail=f"{output_type} is not a text output type")

    return result_text


async def _generate_media_output(topic_id: str, output_type: str, sb) -> dict:
    """Generate a single media output using the existing generator functions."""
    if output_type == "visual_overview_images":
        keys = await generate_images(topic_id, sb)
        return {"type": output_type, "status": "success", "keys": keys}

    elif output_type == "podcast_audio":
        key = await generate_podcast_audio(topic_id, sb)
        return {"type": output_type, "status": "success", "key": key}

    elif output_type == "narration_audio":
        keys = await generate_narration_audio(topic_id, sb)
        return {"type": output_type, "status": "success", "keys": keys}

    else:
        raise HTTPException(status_code=400, detail=f"Unknown media output type: {output_type}")


# ---------------------------------------------------------------------------
# 1. POST /api/admin-topics/create
# ---------------------------------------------------------------------------

@router.post("/admin-topics/create")
async def create_admin_topic(
    body: CreateTopicBody,
    student: dict = Depends(require_admin_student),
):
    """Create a topic with generation_status='idle'. No file upload required."""
    sb = get_supabase()

    # Verify course exists
    course = sb.table("courses").select("id").eq("id", body.course_id).execute()
    if not course.data:
        raise HTTPException(status_code=404, detail="Course not found")

    topic_id = str(uuid.uuid4())

    result = sb.table("topics").insert({
        "id": topic_id,
        "course_id": body.course_id,
        "name": body.name.strip(),
        "generation_status": "idle",
    }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create topic")

    return result.data[0]


# ---------------------------------------------------------------------------
# 2. GET /api/topics/{id}/admin/status
# ---------------------------------------------------------------------------

@router.get("/topics/{topic_id}/admin/status")
async def get_admin_topic_status(
    topic_id: str,
    student: dict = Depends(require_admin_student),
):
    """Return existence info for all 7 output types."""
    sb = get_supabase()
    topic = _get_topic_or_404(sb, topic_id)

    outputs = {}

    # Single-file outputs: check if the URL column is non-null/non-empty
    for output_type in ["learning_asset", "podcast_script", "podcast_audio", "notechart", "visual_overview_script"]:
        col = COLUMN_MAP[output_type]
        val = topic.get(col)
        outputs[output_type] = {
            "exists": bool(val),
            "file_count": 1 if val else 0,
        }

    # Multi-file outputs: check JSONB array length
    for output_type in ["visual_overview_images", "narration_audio"]:
        col = COLUMN_MAP[output_type]
        val = topic.get(col) or []
        outputs[output_type] = {
            "exists": len(val) > 0,
            "file_count": len(val),
        }

    return {
        "topic_id": topic_id,
        "name": topic.get("name"),
        "generation_status": topic.get("generation_status"),
        "outputs": outputs,
    }


# ---------------------------------------------------------------------------
# 2b. GET /api/topics/{id}/admin/view/{type}
# ---------------------------------------------------------------------------

@router.get("/topics/{topic_id}/admin/view/{output_type}")
async def view_admin_output(
    topic_id: str,
    output_type: str,
    student: dict = Depends(require_admin_student),
):
    """Return the text content of a text-based output (learning_asset, podcast_script, notechart, visual_overview_script)."""
    if output_type not in COLUMN_MAP:
        raise HTTPException(status_code=400, detail=f"Unknown output type: {output_type}")

    sb = get_supabase()
    topic = _get_topic_or_404(sb, topic_id)
    col = COLUMN_MAP[output_type]
    val = topic.get(col)

    if not val:
        raise HTTPException(status_code=404, detail=f"{output_type} not generated yet")

    # For text outputs, download and return content
    if output_type in TEXT_OUTPUT_TYPES:
        text = download_from_r2(val).decode("utf-8")
        return {"output_type": output_type, "content": text, "length": len(text)}

    # For array types, return the keys with presigned URLs
    if output_type in ARRAY_COLUMNS:
        keys = val if isinstance(val, list) else []
        urls = [{"key": k, "url": generate_presigned_url(k)} for k in keys]
        return {"output_type": output_type, "files": urls, "count": len(urls)}

    # For single media files, return presigned URL
    url = generate_presigned_url(val)
    return {"output_type": output_type, "url": url, "key": val}


# ---------------------------------------------------------------------------
# 3. PUT /api/topics/{id}/admin/outputs/{type}
# ---------------------------------------------------------------------------

@router.put("/topics/{topic_id}/admin/outputs/{output_type}")
async def upload_admin_output(
    topic_id: str,
    output_type: str,
    files: list[UploadFile] = File(...),
    student: dict = Depends(require_admin_student),
):
    """Upload one or more files for an output type. Stores on R2 and updates the DB column."""
    _validate_output_type(output_type)
    sb = get_supabase()
    _get_topic_or_404(sb, topic_id)

    content_type = CONTENT_TYPE_MAP.get(output_type, "application/octet-stream")

    if output_type in ARRAY_COLUMNS:
        # Multi-file: upload each file, collect keys
        keys = []
        for i, f in enumerate(files, start=1):
            data = await f.read()
            if output_type == "visual_overview_images":
                r2_key = f"{topic_id}/images/slide_{i}.png"
            else:  # narration_audio
                r2_key = f"{topic_id}/narration/slide_{i}.wav"
            upload_bytes_to_r2(r2_key, data, content_type=content_type)
            keys.append(r2_key)

        col = COLUMN_MAP[output_type]
        sb.table("topics").update({col: keys}).eq("id", topic_id).execute()

        return {"output_type": output_type, "keys": keys}

    else:
        # Single-file: take the first uploaded file
        if not files:
            raise HTTPException(status_code=400, detail="At least one file is required")

        data = await files[0].read()
        r2_key = R2_KEY_MAP[output_type].format(topic_id=topic_id)

        # Determine if text or binary
        if output_type in TEXT_OUTPUT_TYPES:
            upload_text_to_r2(r2_key, data.decode("utf-8"))
        else:
            upload_bytes_to_r2(r2_key, data, content_type=content_type)

        col = COLUMN_MAP[output_type]
        sb.table("topics").update({col: r2_key}).eq("id", topic_id).execute()

        return {"output_type": output_type, "key": r2_key}


# ---------------------------------------------------------------------------
# 4. DELETE /api/topics/{id}/admin/outputs/{type}
# ---------------------------------------------------------------------------

@router.delete("/topics/{topic_id}/admin/outputs/{output_type}")
async def delete_admin_output(
    topic_id: str,
    output_type: str,
    student: dict = Depends(require_admin_student),
):
    """Set the DB column for an output type to NULL (single) or empty array (multi)."""
    _validate_output_type(output_type)
    sb = get_supabase()
    _get_topic_or_404(sb, topic_id)

    col = COLUMN_MAP[output_type]

    if output_type in ARRAY_COLUMNS:
        sb.table("topics").update({col: []}).eq("id", topic_id).execute()
    else:
        sb.table("topics").update({col: None}).eq("id", topic_id).execute()

    return {"output_type": output_type, "deleted": True}


# ---------------------------------------------------------------------------
# 5. POST /api/topics/{id}/admin/generate/{type}
# ---------------------------------------------------------------------------

@router.post("/topics/{topic_id}/admin/generate/{output_type}")
async def generate_admin_output(
    topic_id: str,
    output_type: str,
    student: dict = Depends(require_admin_student),
):
    """Generate ONE output in the background. Returns immediately."""
    _validate_output_type(output_type)
    sb = get_supabase()
    _get_topic_or_404(sb, topic_id)

    async def _bg():
        try:
            bg_sb = get_supabase()
            if output_type in TEXT_OUTPUT_TYPES:
                await _generate_text_output(topic_id, output_type, bg_sb, student)
            else:
                await _generate_media_output(topic_id, output_type, bg_sb)
            logger.info(f"admin generate [{topic_id}] — {output_type} completed")
        except Exception as e:
            logger.error(f"admin generate [{topic_id}] — {output_type} failed: {e}")

    asyncio.create_task(_bg())
    return {"output_type": output_type, "status": "started"}


# ---------------------------------------------------------------------------
# 6. POST /api/topics/{id}/admin/generate-test/{type}
# ---------------------------------------------------------------------------

@router.post("/topics/{topic_id}/admin/generate-test/{output_type}")
async def generate_test_output(
    topic_id: str,
    output_type: str,
    body: GenerateTestBody,
    student: dict = Depends(require_admin_student),
):
    """
    Generate a text output using a user-provided test prompt instead of the
    system prompt from get_prompt_for_feature(). Still uses modifiers and
    upstream content. Only for text output types.
    """
    _validate_output_type(output_type)

    if output_type not in TEXT_OUTPUT_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"generate-test is only available for text outputs. '{output_type}' is a media type.",
        )

    sb = get_supabase()
    topic = _get_topic_or_404(sb, topic_id)
    course_id = topic.get("course_id")
    student_id = student.get("id")

    feature_key = FEATURE_KEY_MAP[output_type]

    # Gather modifiers
    modifier_text = gather_modifiers(
        feature=feature_key,
        student_id=student_id,
        course_id=course_id,
        topic_id=topic_id,
    )

    # Read upstream content
    upstream_text = await _read_upstream_text(topic_id, output_type, sb)

    # Build the full prompt with test prompt replacing the base prompt
    if modifier_text:
        full_prompt = (
            f"{body.prompt}\n\n---\n\nMODIFIERS:\n\n{modifier_text}"
            f"\n\n---\n\nSOURCE MATERIAL:\n\n{upstream_text}"
        )
    else:
        full_prompt = f"{body.prompt}\n\n---\n\nSOURCE MATERIAL:\n\n{upstream_text}"

    # Determine model and max_tokens
    if output_type == "learning_asset":
        model = "claude-opus-4-20250514"
        max_tokens = 16384
    elif output_type == "podcast_script":
        model = "claude-sonnet-4-20250514"
        max_tokens = 16384
    else:
        model = "claude-sonnet-4-20250514"
        max_tokens = 8192

    async def _bg_test():
        try:
            bg_sb = get_supabase()
            result_text = await _call_claude(full_prompt, model=model, max_tokens=max_tokens)
            if output_type == "learning_asset":
                await store_learning_asset_result(topic_id, bg_sb, result_text)
            elif output_type == "podcast_script":
                await store_podcast_script_result(topic_id, bg_sb, result_text)
            elif output_type == "notechart":
                await store_notechart_result(topic_id, bg_sb, result_text)
            elif output_type == "visual_overview_script":
                await store_visual_overview_result(topic_id, bg_sb, result_text)
            logger.info(f"admin generate-test [{topic_id}] — {output_type} completed ({len(result_text)} chars)")
        except Exception as e:
            logger.error(f"admin generate-test [{topic_id}] — {output_type} failed: {e}")

    asyncio.create_task(_bg_test())
    return {"output_type": output_type, "status": "started", "model": model}


# ---------------------------------------------------------------------------
# 7. POST /api/topics/{id}/admin/generate-from/{type}
# ---------------------------------------------------------------------------

# Downstream generation chains
DOWNSTREAM_MAP = {
    "learning_asset": [
        "podcast_script",
        "notechart",
        "visual_overview_script",
        "visual_overview_images",
        "podcast_audio",
        "narration_audio",
    ],
    "podcast_script": [
        "podcast_audio",
    ],
    "visual_overview_script": [
        "visual_overview_images",
        "narration_audio",
    ],
}


@router.delete("/topics/{topic_id}/admin/clear-from/{output_type}")
async def clear_downstream(
    topic_id: str,
    output_type: str,
    student: dict = Depends(require_admin_student),
):
    """Clear all downstream outputs from the given type (inclusive)."""
    if output_type not in DOWNSTREAM_MAP:
        raise HTTPException(status_code=400, detail=f"No downstream map for '{output_type}'")

    sb = get_supabase()
    _get_topic_or_404(sb, topic_id)

    to_clear = DOWNSTREAM_MAP[output_type]
    cleared = []

    for step in to_clear:
        col = COLUMN_MAP[step]
        if step in ARRAY_COLUMNS:
            sb.table("topics").update({col: []}).eq("id", topic_id).execute()
        else:
            sb.table("topics").update({col: None}).eq("id", topic_id).execute()
        cleared.append(step)

    return {"cleared": cleared}


@router.post("/topics/{topic_id}/admin/generate-from/{output_type}")
async def generate_downstream(
    topic_id: str,
    output_type: str,
    student: dict = Depends(require_admin_student),
):
    """
    Generate all downstream outputs from the given output type.
    Runs sequentially. Continues on failure. Returns status for each step.
    """
    if output_type not in DOWNSTREAM_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot generate downstream from '{output_type}'. "
                   f"Valid source types: {sorted(DOWNSTREAM_MAP.keys())}",
        )

    sb = get_supabase()
    _get_topic_or_404(sb, topic_id)

    steps = DOWNSTREAM_MAP[output_type]

    async def _bg_downstream():
        bg_sb = get_supabase()
        for step in steps:
            try:
                if step in TEXT_OUTPUT_TYPES:
                    await _generate_text_output(topic_id, step, bg_sb, student)
                else:
                    await _generate_media_output(topic_id, step, bg_sb)
                logger.info(f"generate-from [{topic_id}] — step '{step}' completed")
            except Exception as e:
                logger.error(f"generate-from [{topic_id}] — step '{step}' failed: {e}")
                # Continue with remaining steps

    asyncio.create_task(_bg_downstream())
    return {"source_type": output_type, "status": "started", "steps": steps}
