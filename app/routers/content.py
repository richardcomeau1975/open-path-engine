"""
Content serving endpoints.
Generates presigned URLs for R2-stored content so the browser can load it directly.
"""

import json
import anthropic
from fastapi import APIRouter, Depends, HTTPException, Request
from app.config import settings
from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase
from app.services.file_parser import parse_file
from app.services.r2 import download_from_r2, upload_text_to_r2, upload_bytes_to_r2, generate_presigned_url, generate_presigned_urls
from app.services.prompt_lookup import get_prompt_for_feature

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


@router.get("/api/topics/{topic_id}/quiz")
async def get_quiz(topic_id: str, request: Request):
    """
    Return quiz questions for a topic.
    Generates on first request, caches on R2 for subsequent requests.
    """
    from app.services.generators.quiz import generate_quiz

    supabase = get_supabase()

    # Verify topic exists and has a learning asset
    topic_result = supabase.table("topics").select(
        "id, learning_asset_url"
    ).eq("id", topic_id).execute()

    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    if not topic_result.data[0].get("learning_asset_url"):
        raise HTTPException(status_code=404, detail="No learning asset generated yet")

    # Look up framework_type for this topic's course
    topic_for_fw = supabase.table("topics").select("course_id").eq("id", topic_id).execute()
    framework_type = None
    if topic_for_fw.data and topic_for_fw.data[0].get("course_id"):
        course_res = supabase.table("courses").select("framework_type").eq("id", topic_for_fw.data[0]["course_id"]).execute()
        framework_type = course_res.data[0]["framework_type"] if course_res.data else None

    try:
        questions = await generate_quiz(topic_id, supabase, framework_type=framework_type)
        return {"questions": questions}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Quiz generation failed: {str(e)}")


@router.post("/api/topics/{topic_id}/exam/upload")
async def upload_exam(
    topic_id: str,
    request: Request,
    student: dict = Depends(get_current_student),
):
    """
    Upload a sample exam, analyze it with Sonnet, store the analysis.
    Accepts file upload (PDF, DOCX, PNG, JPG, TXT).
    """
    supabase = get_supabase()

    # Verify topic exists
    topic_result = supabase.table("topics").select("id, course_id").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    # Get the uploaded file
    form = await request.form()
    file = form.get("file")
    if not file:
        raise HTTPException(status_code=400, detail="No file uploaded")

    filename = file.filename
    file_bytes = await file.read()

    # Store the original exam on R2
    exam_key = f"{topic_id}/exam/{filename}"
    upload_bytes_to_r2(exam_key, file_bytes, content_type=file.content_type or "application/octet-stream")

    # Parse text from the exam
    try:
        exam_text = parse_file(filename, file_bytes)
    except ValueError:
        exam_text = f"[Uploaded image file: {filename} — {len(file_bytes)} bytes. Unable to extract text from image.]"

    # Look up framework_type for this topic's course
    framework_type = None
    course_id = topic_result.data[0].get("course_id")
    if course_id:
        course_res = supabase.table("courses").select("framework_type").eq("id", course_id).execute()
        framework_type = course_res.data[0]["framework_type"] if course_res.data else None

    # Load exam analysis prompt (framework-aware lookup)
    base_prompt = get_prompt_for_feature("exam_analysis", framework_type)

    # Call Sonnet with streaming
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    analysis_text = ""
    with client.messages.stream(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": f"{base_prompt}\n\n---\n\nSAMPLE EXAM CONTENT:\n\n{exam_text}"
        }]
    ) as stream:
        for text in stream.text_stream:
            analysis_text += text

    # Store analysis on R2
    analysis_key = f"{topic_id}/exam_analysis.md"
    upload_text_to_r2(analysis_key, analysis_text)

    # Upsert modifier
    student_id = student["id"]
    course_id = topic_result.data[0].get("course_id")

    if course_id:
        existing = supabase.table("modifiers").select("id").eq(
            "student_id", student_id
        ).eq("course_id", course_id).eq("modifier_type", "testing_profile").limit(1).execute()

        if existing.data:
            supabase.table("modifiers").update({
                "content": analysis_text,
            }).eq("id", existing.data[0]["id"]).execute()
        else:
            supabase.table("modifiers").insert({
                "student_id": student_id,
                "course_id": course_id,
                "topic_id": topic_id,
                "modifier_type": "testing_profile",
                "content": analysis_text,
            }).execute()

    return {
        "analysis": analysis_text,
        "exam_file": exam_key,
        "analysis_file": analysis_key,
    }


@router.get("/api/topics/{topic_id}/exam/analysis")
async def get_exam_analysis(topic_id: str, request: Request):
    """Return stored exam analysis if it exists."""
    analysis_key = f"{topic_id}/exam_analysis.md"

    try:
        analysis = download_from_r2(analysis_key).decode("utf-8")
        return {"analysis": analysis, "exists": True}
    except Exception:
        return {"analysis": None, "exists": False}


@router.get("/api/topics/{topic_id}/learning-asset")
async def get_learning_asset(topic_id: str, request: Request):
    """Return the learning asset text."""
    supabase = get_supabase()
    topic_result = supabase.table("topics").select("id, learning_asset_url").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = topic_result.data[0]
    if not topic.get("learning_asset_url"):
        raise HTTPException(status_code=404, detail="No learning asset generated yet")

    text = download_from_r2(topic["learning_asset_url"]).decode("utf-8")
    return {"text": text}
