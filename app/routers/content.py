"""
Content serving endpoints.
Generates presigned URLs for R2-stored content so the browser can load it directly.
"""

from fastapi import APIRouter, HTTPException, Request
from app.services.supabase import get_supabase
from app.services.r2 import generate_presigned_url, generate_presigned_urls

router = APIRouter()


@router.get("/api/topics/{topic_id}/content")
async def get_topic_content(topic_id: str, request: Request):
    """
    Return presigned URLs for all generated content for a topic.
    The frontend uses these URLs to load images, audio, and text directly from R2.
    """
    supabase = get_supabase()

    result = supabase.table("topics").select(
        "id, name, week_number, generation_status, "
        "learning_asset_url, podcast_script_url, podcast_audio_url, "
        "notechart_url, visual_overview_script_url, visual_overview_images, "
        "visual_overview_audio_urls"
    ).eq("id", topic_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = result.data[0]

    # Build presigned URLs for all content
    content = {
        "topic_id": topic["id"],
        "name": topic["name"],
        "week_number": topic.get("week_number"),
        "generation_status": topic["generation_status"],
    }

    # Single files
    if topic.get("learning_asset_url"):
        content["learning_asset"] = generate_presigned_url(topic["learning_asset_url"])

    if topic.get("podcast_script_url"):
        content["podcast_script"] = generate_presigned_url(topic["podcast_script_url"])

    if topic.get("podcast_audio_url"):
        content["podcast_audio"] = generate_presigned_url(topic["podcast_audio_url"])

    if topic.get("notechart_url"):
        content["notechart"] = generate_presigned_url(topic["notechart_url"])

    if topic.get("visual_overview_script_url"):
        content["visual_overview_script"] = generate_presigned_url(topic["visual_overview_script_url"])

    # Image arrays
    images = topic.get("visual_overview_images") or []
    if images:
        content["visual_overview_images"] = [
            {"key": key, "url": generate_presigned_url(key)} for key in images
        ]

    # Audio arrays
    audio_urls = topic.get("visual_overview_audio_urls") or []
    if audio_urls:
        content["visual_overview_audio"] = [
            {"key": key, "url": generate_presigned_url(key)} for key in audio_urls
        ]

    return content


@router.get("/api/content/presign")
async def presign_single(key: str, request: Request):
    """
    Generate a presigned URL for a single R2 key.
    Useful for on-demand content loading.
    """
    if not key:
        raise HTTPException(status_code=400, detail="key parameter required")

    try:
        url = generate_presigned_url(key)
        return {"key": key, "url": url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate presigned URL: {str(e)}")
