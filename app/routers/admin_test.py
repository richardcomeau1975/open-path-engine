"""
Admin Test Environment — manual replace + selective re-generation.
Allows admin to replace any pipeline output and re-run individual steps.
"""

import asyncio
import logging
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form
from app.routers.admin import require_admin
from app.services.supabase import get_supabase
from app.services.r2 import upload_text_to_r2, upload_bytes_to_r2, download_from_r2, generate_presigned_url
from app.services.batch_api import run_anthropic_batch
from app.services.generators.learning_asset import (
    build_learning_asset_prompt, store_learning_asset_result, MODEL as LA_MODEL, MAX_TOKENS as LA_MAX_TOKENS,
)
from app.services.generators.podcast_script import (
    build_podcast_script_prompt, store_podcast_script_result, MODEL as PS_MODEL, MAX_TOKENS as PS_MAX_TOKENS,
)
from app.services.generators.notechart import (
    build_notechart_prompt, store_notechart_result, MODEL as NC_MODEL, MAX_TOKENS as NC_MAX_TOKENS,
)
from app.services.generators.visual_overview import (
    build_visual_overview_prompt, store_visual_overview_result, MODEL as VO_MODEL, MAX_TOKENS as VO_MAX_TOKENS,
)
from app.services.generators.images import generate_images as gen_images
from app.services.generators.podcast_audio import generate_podcast_audio as gen_podcast_audio
from app.services.generators.narration_audio import generate_narration_audio as gen_narration_audio

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin", tags=["admin-test"])

# Map output_type -> topics table column
OUTPUT_COLUMNS = {
    "learning_asset": "learning_asset_url",
    "podcast_script": "podcast_script_url",
    "podcast_audio": "podcast_audio_url",
    "notechart": "notechart_url",
    "visual_overview_script": "visual_overview_script_url",
    "visual_overview_images": "visual_overview_images",
    "visual_overview_audio": "visual_overview_audio_urls",
}

# R2 key patterns per output_type
def r2_key(topic_id: str, output_type: str, filename: str = None, index: int = 0):
    patterns = {
        "learning_asset": f"{topic_id}/learning_asset.md",
        "podcast_script": f"{topic_id}/podcast_script.md",
        "podcast_audio": f"{topic_id}/podcast_audio.wav",
        "notechart": f"{topic_id}/notechart.json",
        "visual_overview_script": f"{topic_id}/visual_overview_script.json",
    }
    if output_type in patterns:
        return patterns[output_type]
    return None


def _get_gen_kwargs(sb, topic_id):
    """Get framework_type, student_id, course_id for a topic."""
    topic = sb.table("topics").select("course_id").eq("id", topic_id).execute()
    course_id = topic.data[0]["course_id"] if topic.data else None
    framework_type = None
    student_id = None
    if course_id:
        course = sb.table("courses").select("framework_type, student_id").eq("id", course_id).execute()
        if course.data:
            framework_type = course.data[0].get("framework_type")
            student_id = course.data[0].get("student_id")
    return dict(framework_type=framework_type, student_id=student_id, course_id=course_id)


# ── Get topic outputs status ──────────────────────────

@router.get("/topics/{topic_id}/outputs", dependencies=[Depends(require_admin)])
async def get_topic_outputs(topic_id: str):
    """Return current state of all pipeline outputs for a topic."""
    sb = get_supabase()
    result = sb.table("topics").select(
        "id, name, course_id, parsed_text_url, "
        "learning_asset_url, podcast_script_url, podcast_audio_url, "
        "notechart_url, visual_overview_script_url, visual_overview_images, "
        "visual_overview_audio_urls, generation_status"
    ).eq("id", topic_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = result.data[0]

    # Get course + student info
    course_info = {}
    if topic.get("course_id"):
        cr = sb.table("courses").select("name, student_id").eq("id", topic["course_id"]).execute()
        if cr.data:
            course_info["course_name"] = cr.data[0]["name"]
            sid = cr.data[0].get("student_id")
            if sid:
                sr = sb.table("students").select("name").eq("id", sid).execute()
                if sr.data:
                    course_info["student_name"] = sr.data[0]["name"]

    # Build output cards
    outputs = []

    def add_output(key, label, column, is_array=False):
        val = topic.get(column)
        exists = bool(val) if not is_array else bool(val and len(val) > 0)
        url = None
        if exists and not is_array:
            try:
                url = generate_presigned_url(val)
            except:
                pass
        urls = None
        if exists and is_array:
            try:
                urls = [generate_presigned_url(k) for k in val]
            except:
                pass
        outputs.append({
            "key": key,
            "label": label,
            "exists": exists,
            "r2_key": val if not is_array else None,
            "r2_keys": val if is_array else None,
            "url": url,
            "urls": urls,
            "count": len(val) if is_array and val else None,
        })

    add_output("learning_asset", "Learning Asset", "learning_asset_url")
    add_output("podcast_script", "Podcast Script", "podcast_script_url")
    add_output("podcast_audio", "Podcast Audio", "podcast_audio_url")
    add_output("notechart", "Active Recall", "notechart_url")
    add_output("visual_overview_script", "Visual Overview Script", "visual_overview_script_url")
    add_output("visual_overview_images", "Visual Overview Images", "visual_overview_images", is_array=True)
    add_output("visual_overview_audio", "Visual Overview Audio", "visual_overview_audio_urls", is_array=True)

    return {
        "topic": {
            "id": topic["id"],
            "name": topic["name"],
            "generation_status": topic["generation_status"],
            "has_source": bool(topic.get("parsed_text_url")),
        },
        "student_name": course_info.get("student_name", "Unknown"),
        "course_name": course_info.get("course_name", "Unknown"),
        "outputs": outputs,
    }


# ── Replace an output ─────────────────────────────────

@router.put("/topics/{topic_id}/outputs/{output_type}", dependencies=[Depends(require_admin)])
async def replace_output(topic_id: str, output_type: str, file: UploadFile = File(...)):
    """Replace a single pipeline output by uploading a file."""
    if output_type not in OUTPUT_COLUMNS:
        raise HTTPException(status_code=400, detail=f"Unknown output_type: {output_type}")

    sb = get_supabase()
    topic = sb.table("topics").select("id").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    file_bytes = await file.read()
    column = OUTPUT_COLUMNS[output_type]

    if output_type == "visual_overview_images":
        # For images, this replaces a single image — use index from filename or append
        # Actually, accept multiple calls; store under standard path
        # For simplicity: replace all images with this one upload
        # Better: use a separate multi-file endpoint. For now, single image replacement.
        raise HTTPException(status_code=400, detail="Use the multi-image endpoint for visual_overview_images")

    if output_type == "visual_overview_audio":
        raise HTTPException(status_code=400, detail="Use the multi-audio endpoint for visual_overview_audio")

    # Single file outputs
    key = r2_key(topic_id, output_type)
    if not key:
        raise HTTPException(status_code=400, detail=f"Cannot determine R2 key for {output_type}")

    # Determine content type
    ct_map = {
        "learning_asset": "text/plain",
        "podcast_script": "text/plain",
        "podcast_audio": "audio/wav",
        "notechart": "application/json",
        "visual_overview_script": "application/json",
    }
    content_type = ct_map.get(output_type, "application/octet-stream")

    upload_bytes_to_r2(key, file_bytes, content_type)

    # Update topics table
    sb.table("topics").update({column: key}).eq("id", topic_id).execute()

    return {"status": "replaced", "output_type": output_type, "r2_key": key}


# ── Replace multiple images ──────────────────────────

@router.put("/topics/{topic_id}/outputs/visual_overview_images/multi", dependencies=[Depends(require_admin)])
async def replace_images(topic_id: str, request: Request):
    """Replace visual overview images. Accepts multipart form with multiple 'files' fields."""
    from starlette.datastructures import UploadFile as StarletteUploadFile

    sb = get_supabase()
    topic = sb.table("topics").select("id").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    form = await request.form()
    files = form.getlist("files")

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    keys = []
    for i, f in enumerate(files):
        data = await f.read()
        ext = f.filename.rsplit(".", 1)[-1] if "." in f.filename else "png"
        key = f"{topic_id}/images/slide_{i}.{ext}"
        ct = "image/png" if ext == "png" else "image/jpeg" if ext in ("jpg", "jpeg") else "image/webp"
        upload_bytes_to_r2(key, data, ct)
        keys.append(key)

    sb.table("topics").update({"visual_overview_images": keys}).eq("id", topic_id).execute()

    return {"status": "replaced", "output_type": "visual_overview_images", "count": len(keys), "keys": keys}


# ── Generate a single step ────────────────────────────

@router.post("/topics/{topic_id}/generate/{output_type}", dependencies=[Depends(require_admin)])
async def generate_single_output(topic_id: str, output_type: str):
    """Re-generate a single pipeline output using its current upstream input."""
    if output_type not in OUTPUT_COLUMNS and output_type != "visual_overview_images" and output_type != "visual_overview_audio":
        raise HTTPException(status_code=400, detail=f"Unknown output_type: {output_type}")

    sb = get_supabase()
    topic = sb.table("topics").select(
        "id, parsed_text_url, learning_asset_url, podcast_script_url, "
        "visual_overview_script_url"
    ).eq("id", topic_id).execute()

    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic_data = topic.data[0]
    gen_kwargs = _get_gen_kwargs(sb, topic_id)

    try:
        if output_type == "learning_asset":
            prompt = await build_learning_asset_prompt(topic_id, sb, **gen_kwargs)
            results = await run_anthropic_batch([{
                "custom_id": "learning_asset",
                "model": LA_MODEL,
                "max_tokens": LA_MAX_TOKENS,
                "prompt": prompt,
            }])
            text = results.get("learning_asset")
            if not text:
                raise Exception("Learning asset generation failed")
            await store_learning_asset_result(topic_id, sb, text)
            return {"status": "generated", "output_type": output_type, "chars": len(text)}

        elif output_type == "podcast_script":
            la_key = topic_data.get("learning_asset_url")
            if not la_key:
                raise HTTPException(status_code=400, detail="No learning asset exists — generate it first")
            la_text = download_from_r2(la_key).decode("utf-8")
            prompt = await build_podcast_script_prompt(topic_id, sb, la_text, **gen_kwargs)
            results = await run_anthropic_batch([{
                "custom_id": "podcast_script",
                "model": PS_MODEL,
                "max_tokens": PS_MAX_TOKENS,
                "prompt": prompt,
            }])
            text = results.get("podcast_script")
            if not text:
                raise Exception("Podcast script generation failed")
            await store_podcast_script_result(topic_id, sb, text)
            return {"status": "generated", "output_type": output_type, "chars": len(text)}

        elif output_type == "notechart":
            la_key = topic_data.get("learning_asset_url")
            if not la_key:
                raise HTTPException(status_code=400, detail="No learning asset exists — generate it first")
            la_text = download_from_r2(la_key).decode("utf-8")
            prompt = await build_notechart_prompt(topic_id, sb, la_text, **gen_kwargs)
            results = await run_anthropic_batch([{
                "custom_id": "notechart",
                "model": NC_MODEL,
                "max_tokens": NC_MAX_TOKENS,
                "prompt": prompt,
            }])
            text = results.get("notechart")
            if not text:
                raise Exception("Notechart generation failed")
            await store_notechart_result(topic_id, sb, text)
            return {"status": "generated", "output_type": output_type, "chars": len(text)}

        elif output_type == "visual_overview_script":
            la_key = topic_data.get("learning_asset_url")
            if not la_key:
                raise HTTPException(status_code=400, detail="No learning asset exists — generate it first")
            la_text = download_from_r2(la_key).decode("utf-8")
            prompt = await build_visual_overview_prompt(topic_id, sb, la_text, **gen_kwargs)
            results = await run_anthropic_batch([{
                "custom_id": "visual_overview",
                "model": VO_MODEL,
                "max_tokens": VO_MAX_TOKENS,
                "prompt": prompt,
            }])
            text = results.get("visual_overview")
            if not text:
                raise Exception("Visual overview script generation failed")
            await store_visual_overview_result(topic_id, sb, text)
            return {"status": "generated", "output_type": output_type, "chars": len(text)}

        elif output_type == "visual_overview_images":
            vo_key = topic_data.get("visual_overview_script_url")
            if not vo_key:
                raise HTTPException(status_code=400, detail="No visual overview script — generate it first")
            await gen_images(topic_id, sb)
            return {"status": "generated", "output_type": output_type}

        elif output_type == "podcast_audio":
            ps_key = topic_data.get("podcast_script_url")
            if not ps_key:
                raise HTTPException(status_code=400, detail="No podcast script — generate it first")
            await gen_podcast_audio(topic_id, sb)
            return {"status": "generated", "output_type": output_type}

        elif output_type == "visual_overview_audio":
            vo_key = topic_data.get("visual_overview_script_url")
            if not vo_key:
                raise HTTPException(status_code=400, detail="No visual overview script — generate it first")
            await gen_narration_audio(topic_id, sb)
            return {"status": "generated", "output_type": output_type}

        else:
            raise HTTPException(status_code=400, detail=f"Cannot generate {output_type}")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Generate single [{topic_id}/{output_type}] failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Generate all downstream from learning asset ──────

@router.post("/topics/{topic_id}/generate-downstream", dependencies=[Depends(require_admin)])
async def generate_downstream(topic_id: str):
    """Re-generate everything downstream of the learning asset."""
    sb = get_supabase()
    topic = sb.table("topics").select(
        "id, learning_asset_url"
    ).eq("id", topic_id).execute()

    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    la_key = topic.data[0].get("learning_asset_url")
    if not la_key:
        raise HTTPException(status_code=400, detail="No learning asset — generate or upload it first")

    gen_kwargs = _get_gen_kwargs(sb, topic_id)
    la_text = download_from_r2(la_key).decode("utf-8")

    results_log = []
    errors = []

    try:
        # Batch: podcast_script + notechart + visual_overview_script
        ps_prompt = await build_podcast_script_prompt(topic_id, sb, la_text, **gen_kwargs)
        nc_prompt = await build_notechart_prompt(topic_id, sb, la_text, **gen_kwargs)
        vo_prompt = await build_visual_overview_prompt(topic_id, sb, la_text, **gen_kwargs)

        batch_results = await run_anthropic_batch([
            {"custom_id": "podcast_script", "model": PS_MODEL, "max_tokens": PS_MAX_TOKENS, "prompt": ps_prompt},
            {"custom_id": "notechart", "model": NC_MODEL, "max_tokens": NC_MAX_TOKENS, "prompt": nc_prompt},
            {"custom_id": "visual_overview", "model": VO_MODEL, "max_tokens": VO_MAX_TOKENS, "prompt": vo_prompt},
        ])

        ps_text = batch_results.get("podcast_script")
        nc_text = batch_results.get("notechart")
        vo_text = batch_results.get("visual_overview")

        if ps_text:
            await store_podcast_script_result(topic_id, sb, ps_text)
            results_log.append("podcast_script")
        else:
            errors.append("podcast_script failed")

        if nc_text:
            await store_notechart_result(topic_id, sb, nc_text)
            results_log.append("notechart")
        else:
            errors.append("notechart failed")

        if vo_text:
            await store_visual_overview_result(topic_id, sb, vo_text)
            results_log.append("visual_overview_script")
        else:
            errors.append("visual_overview_script failed")

        # Phase 3: images, podcast audio, narration audio (concurrent)
        phase3 = []
        if ps_text:
            phase3.append(("podcast_audio", gen_podcast_audio(topic_id, sb)))
        if vo_text:
            phase3.append(("visual_overview_images", gen_images(topic_id, sb)))
            phase3.append(("visual_overview_audio", gen_narration_audio(topic_id, sb)))

        if phase3:
            phase3_results = await asyncio.gather(
                *[t for _, t in phase3],
                return_exceptions=True,
            )
            for (name, _), res in zip(phase3, phase3_results):
                if isinstance(res, Exception):
                    errors.append(f"{name}: {str(res)}")
                else:
                    results_log.append(name)

    except Exception as e:
        errors.append(str(e))

    return {
        "status": "completed" if not errors else "partial",
        "generated": results_log,
        "errors": errors,
    }
