"""
Content serving endpoints.
Generates presigned URLs for R2-stored content so the browser can load it directly.
"""

import json
from fastapi import APIRouter, Depends, HTTPException, Request
from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase
from app.services.r2 import download_from_r2, generate_presigned_url, generate_presigned_urls

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


@router.get("/api/topics/{topic_id}/notechart/questions")
async def get_notechart_questions(
    topic_id: str,
    request: Request,
    student: dict = Depends(get_current_student),
):
    """
    Return the note chart questions and any saved answers for the current student.
    Questions come from the generated notechart JSON on R2.
    Answers come from Supabase.
    """
    supabase = get_supabase()

    # Get topic info
    topic_result = supabase.table("topics").select("id, notechart_url").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = topic_result.data[0]
    if not topic.get("notechart_url"):
        raise HTTPException(status_code=404, detail="No note chart generated yet")

    # Download and parse questions
    raw = download_from_r2(topic["notechart_url"]).decode("utf-8")

    # Strip markdown code fences if present
    clean = raw.strip()
    if clean.startswith("```"):
        first_nl = clean.index("\n")
        clean = clean[first_nl + 1:]
    if clean.endswith("```"):
        clean = clean[:-3]
    clean = clean.strip()

    try:
        questions = json.loads(clean)
    except json.JSONDecodeError:
        questions = [{"section": "Questions", "question": clean}]

    # Get saved answers for this student
    student_id = student["id"]
    saved_answers = {}
    answers_result = supabase.table("note_chart_answers").select(
        "question, answer"
    ).eq("topic_id", topic_id).eq("student_id", student_id).execute()
    saved_answers = {a["question"]: a["answer"] for a in answers_result.data}

    # Merge answers into questions
    for q in questions:
        q["answer"] = saved_answers.get(q.get("question", ""), "")

    return {"questions": questions}


@router.post("/api/topics/{topic_id}/notechart/save")
async def save_notechart_answers(
    topic_id: str,
    request: Request,
    student: dict = Depends(get_current_student),
):
    """
    Save note chart answers for the current student.
    Body: { "answers": [{"section": "...", "question": "...", "answer": "..."}] }
    Uses upsert to create or update.
    """
    supabase = get_supabase()

    body = await request.json()
    answers = body.get("answers", [])

    if not answers:
        return {"saved": 0}

    student_id = student["id"]

    # Upsert each answer
    saved_count = 0
    for item in answers:
        question = item.get("question", "").strip()
        answer = item.get("answer", "").strip()
        section = item.get("section", "")

        if not question:
            continue

        supabase.table("note_chart_answers").upsert({
            "topic_id": topic_id,
            "student_id": student_id,
            "section": section,
            "question": question,
            "answer": answer,
        }, on_conflict="topic_id,student_id,question").execute()
        saved_count += 1

    return {"saved": saved_count}
