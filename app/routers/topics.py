import uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from typing import List, Optional
from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase
from app.services.r2 import get_r2_client, upload_text_to_r2
from app.services.file_parser import parse_multiple_files
from app.config import settings

router = APIRouter(prefix="/api", tags=["topics"])

ALLOWED_EXTENSIONS = {".pdf", ".pptx", ".docx", ".xlsx", ".txt", ".md"}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB


@router.get("/courses/{course_id}/topics")
async def list_topics(course_id: str, student: dict = Depends(get_current_student)):
    sb = get_supabase()

    # Verify the course belongs to this student
    course = sb.table("courses").select("id").eq("id", course_id).eq("student_id", student["id"]).execute()
    if not course.data:
        raise HTTPException(status_code=404, detail="Course not found")

    result = sb.table("topics").select("*").eq("course_id", course_id).order("week_number", desc=False).execute()
    return result.data


@router.get("/topics/{topic_id}/dashboard")
async def get_topic_dashboard(topic_id: str, student: dict = Depends(get_current_student)):
    sb = get_supabase()

    # Get topic and verify ownership through course
    topic = sb.table("topics").select("*, courses(student_id)").eq("id", topic_id).execute()
    if not topic.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic_data = topic.data[0]
    course_info = topic_data.get("courses")
    if not course_info or course_info.get("student_id") != student["id"]:
        raise HTTPException(status_code=404, detail="Topic not found")

    # Get progress for this topic
    progress = sb.table("progress").select("feature, state").eq("topic_id", topic_id).eq("student_id", student["id"]).execute()
    progress_map = {p["feature"]: p["state"] for p in progress.data}

    features = [
        {"number": 1, "key": "visual_overview", "name": "Visual Overview", "description": "Build Your Foundation"},
        {"number": 2, "key": "podcast", "name": "Podcast", "description": "Listen & Explore"},
        {"number": 3, "key": "walkthrough", "name": "Knowledge Walkthrough", "description": "Think It Through"},
        {"number": 4, "key": "note_chart", "name": "Note Chart", "description": "Test Your Recall"},
        {"number": 5, "key": "how_tested", "name": "How You're Tested", "description": "Know the Format"},
        {"number": 6, "key": "test_me", "name": "Test Me", "description": "Check Your Understanding"},
    ]

    for feature in features:
        feature["state"] = progress_map.get(feature["key"], "not_available")
        if topic_data.get("generation_status") != "complete":
            feature["state"] = "not_available"

    return {
        "topic": {
            "id": topic_data["id"],
            "name": topic_data["name"],
            "week_number": topic_data.get("week_number"),
            "generation_status": topic_data.get("generation_status", "none"),
            "course_id": topic_data["course_id"],
        },
        "features": features,
    }


@router.post("/topics")
async def create_topic_with_upload(
    course_id: str = Form(...),
    name: str = Form(...),
    week_number: Optional[int] = Form(None),
    files: List[UploadFile] = File(...),
    student: dict = Depends(get_current_student),
):
    sb = get_supabase()

    # Verify the course belongs to this student
    course = sb.table("courses").select("id").eq("id", course_id).eq("student_id", student["id"]).execute()
    if not course.data:
        raise HTTPException(status_code=404, detail="Course not found")

    # Validate files
    if len(files) > 10:
        raise HTTPException(status_code=400, detail="Maximum 10 files allowed")

    for f in files:
        # Check extension
        filename = f.filename or ""
        ext = ""
        if "." in filename:
            ext = "." + filename.rsplit(".", 1)[1].lower()
        if ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(
                status_code=400,
                detail=f"File type not allowed: {filename}. Accepted: PDF, PPTX, DOCX, XLSX, TXT, MD",
            )

    # Create topic row
    topic_id = str(uuid.uuid4())
    r2_prefix = f"{student['id']}/{course_id}/{topic_id}/uploads"

    topic_result = sb.table("topics").insert({
        "id": topic_id,
        "course_id": course_id,
        "name": name.strip(),
        "week_number": week_number,
        "generation_status": "none",
        "r2_prefix": r2_prefix,
    }).execute()

    if not topic_result.data:
        raise HTTPException(status_code=500, detail="Failed to create topic")

    # Upload files to R2
    r2 = get_r2_client()
    uploaded_files = []
    file_pairs = []  # (filename, bytes) for parsing

    for f in files:
        file_content = await f.read()

        # Check file size
        if len(file_content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail=f"File too large: {f.filename}. Maximum 50MB.")

        r2_key = f"{r2_prefix}/{f.filename}"

        r2.put_object(
            Bucket=settings.R2_BUCKET_NAME,
            Key=r2_key,
            Body=file_content,
            ContentType=f.content_type or "application/octet-stream",
        )

        uploaded_files.append({
            "filename": f.filename,
            "r2_key": r2_key,
            "size": len(file_content),
        })

        file_pairs.append((f.filename, file_content))

    # Parse uploaded files and store concatenated text on R2
    parsed_text = parse_multiple_files(file_pairs)
    parsed_text_key = f"{topic_id}/parsed_text.txt"
    upload_text_to_r2(r2, parsed_text_key, parsed_text)

    # Update topic with parsed text URL
    sb.table("topics").update({
        "parsed_text_url": parsed_text_key,
    }).eq("id", topic_id).execute()

    return {
        "topic": topic_result.data[0],
        "uploaded_files": uploaded_files,
    }
