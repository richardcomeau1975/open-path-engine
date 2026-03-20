import secrets
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from app.config import settings
from app.services.supabase import get_supabase

router = APIRouter(prefix="/api/admin", tags=["admin"])

_admin_tokens = set()


def require_admin(request: Request):
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing admin token")
    token = auth_header.split(" ", 1)[1]
    if token not in _admin_tokens:
        raise HTTPException(status_code=401, detail="Invalid admin token")
    return True


# ── Auth ──────────────────────────────────────────────

@router.post("/login")
async def admin_login(request: Request):
    body = await request.json()
    password = body.get("password", "")
    if password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")
    token = secrets.token_urlsafe(32)
    _admin_tokens.add(token)
    return {"token": token}


# ── Students ──────────────────────────────────────────

@router.get("/students", dependencies=[Depends(require_admin)])
async def list_students():
    sb = get_supabase()
    result = sb.table("students").select("*").is_("archived_at", "null").order("created_at", desc=True).execute()
    return result.data


@router.post("/students", dependencies=[Depends(require_admin)])
async def create_student(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    email = body.get("email", "").strip()
    phone = body.get("phone", "").strip()

    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    if not email and not phone:
        raise HTTPException(status_code=400, detail="Email or phone is required")

    sb = get_supabase()

    if email:
        existing = sb.table("students").select("id").eq("email", email).execute()
        if existing.data:
            raise HTTPException(status_code=409, detail="Student with this email already exists")

    name_parts = name.split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    clerk_payload = {"first_name": first_name, "last_name": last_name}
    if email:
        clerk_payload["email_address"] = [email]
    if phone:
        if not phone.startswith("+"):
            phone = f"+1{phone}"
        clerk_payload["phone_number"] = [phone]

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://api.clerk.com/v1/users",
                json=clerk_payload,
                headers={
                    "Authorization": f"Bearer {settings.CLERK_SECRET_KEY}",
                    "Content-Type": "application/json",
                },
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Clerk API error: {resp.text}")
            clerk_user = resp.json()
            clerk_id = clerk_user["id"]
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach Clerk: {str(e)}")

    result = sb.table("students").insert({
        "clerk_id": clerk_id,
        "name": name,
        "email": email or None,
    }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create student")

    return result.data[0]


@router.post("/students/{student_id}/archive", dependencies=[Depends(require_admin)])
async def archive_student(student_id: str):
    sb = get_supabase()
    student = sb.table("students").select("id").eq("id", student_id).execute()
    if not student.data:
        raise HTTPException(status_code=404, detail="Student not found")
    sb.table("students").update({"archived_at": "now()"}).eq("id", student_id).execute()
    return {"status": "archived"}


# ── Courses ───────────────────────────────────────────

@router.get("/courses", dependencies=[Depends(require_admin)])
async def list_courses():
    sb = get_supabase()
    result = sb.table("courses").select("*, students(name, email)").eq("active", True).order("created_at", desc=True).execute()
    return result.data


@router.post("/courses", dependencies=[Depends(require_admin)])
async def create_course(request: Request):
    body = await request.json()
    student_id = body.get("student_id", "").strip()
    name = body.get("name", "").strip()
    framework_type = body.get("framework_type", "").strip() or None

    if not student_id:
        raise HTTPException(status_code=400, detail="student_id is required")
    if not name:
        raise HTTPException(status_code=400, detail="Course name is required")

    sb = get_supabase()
    student = sb.table("students").select("id").eq("id", student_id).execute()
    if not student.data:
        raise HTTPException(status_code=404, detail="Student not found")

    result = sb.table("courses").insert({
        "student_id": student_id,
        "name": name,
        "framework_type": framework_type,
    }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create course")

    return result.data[0]


@router.post("/courses/{course_id}/archive", dependencies=[Depends(require_admin)])
async def archive_course(course_id: str):
    sb = get_supabase()
    course = sb.table("courses").select("id").eq("id", course_id).execute()
    if not course.data:
        raise HTTPException(status_code=404, detail="Course not found")
    sb.table("courses").update({"active": False, "archived_at": "now()"}).eq("id", course_id).execute()
    return {"status": "archived"}


# ── Topics (admin view) ──────────────────────────────

@router.get("/courses/{course_id}/topics", dependencies=[Depends(require_admin)])
async def list_course_topics(course_id: str):
    sb = get_supabase()
    result = sb.table("topics").select("*").eq("course_id", course_id).order("week_number", desc=False).execute()
    return result.data


# ── Modifiers ─────────────────────────────────────────

MODIFIER_TYPES = [
    {"type": "testing_profile", "scope": "course", "label": "Testing Profile", "description": "How this student gets tested in this course"},
    {"type": "engagement_profile", "scope": "course", "label": "Engagement Profile", "description": "What hooks this student in this material"},
    {"type": "note_profile", "scope": "course", "label": "Note Profile", "description": "How this student takes notes"},
    {"type": "syllabus_context", "scope": "course", "label": "Syllabus Context", "description": "Extracted from syllabus upload"},
    {"type": "learning_preferences", "scope": "student", "label": "Learning Preferences", "description": "Global learning style preferences"},
]


@router.get("/modifier-types", dependencies=[Depends(require_admin)])
async def get_modifier_types():
    return MODIFIER_TYPES


@router.get("/modifiers", dependencies=[Depends(require_admin)])
async def list_modifiers(student_id: str = None, course_id: str = None):
    sb = get_supabase()
    query = sb.table("modifiers").select("*")
    if student_id:
        query = query.eq("student_id", student_id)
    if course_id:
        query = query.eq("course_id", course_id)
    result = query.order("created_at", desc=True).execute()
    return result.data


@router.post("/modifiers", dependencies=[Depends(require_admin)])
async def create_or_update_modifier(request: Request):
    body = await request.json()
    student_id = body.get("student_id")
    course_id = body.get("course_id")
    modifier_type = body.get("modifier_type", "").strip()
    content = body.get("content", "").strip()

    if not modifier_type:
        raise HTTPException(status_code=400, detail="modifier_type is required")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    sb = get_supabase()

    # Check if modifier already exists for this scope
    query = sb.table("modifiers").select("id").eq("modifier_type", modifier_type)
    if student_id:
        query = query.eq("student_id", student_id)
    if course_id:
        query = query.eq("course_id", course_id)
    existing = query.execute()

    if existing.data:
        # Update existing
        result = sb.table("modifiers").update({
            "content": content,
            "updated_at": "now()",
        }).eq("id", existing.data[0]["id"]).execute()
    else:
        # Create new
        result = sb.table("modifiers").insert({
            "student_id": student_id,
            "course_id": course_id,
            "modifier_type": modifier_type,
            "content": content,
        }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to save modifier")

    return result.data[0]


@router.delete("/modifiers/{modifier_id}", dependencies=[Depends(require_admin)])
async def delete_modifier(modifier_id: str):
    sb = get_supabase()
    sb.table("modifiers").delete().eq("id", modifier_id).execute()
    return {"status": "deleted"}


# ── Prompts ───────────────────────────────────────────

PROMPT_SOCKETS = [
    {"feature": "learning_asset_generator", "label": "Learning Asset Generator", "description": "Generates the learning asset from uploaded materials (Opus)"},
    {"feature": "walkthrough", "label": "Knowledge Walkthrough", "description": "Constructivist tutor prompt (Sonnet with caching)"},
    {"feature": "podcast_generator", "label": "Podcast Generator", "description": "Generates podcast script from learning asset (Sonnet)"},
    {"feature": "visual_overview", "label": "Visual Overview", "description": "Generates narration script + image prompts (Sonnet)"},
    {"feature": "notechart", "label": "Note Chart", "description": "Generates framework-shaped recall questions (Sonnet)"},
    {"feature": "exam_analysis", "label": "Exam Analysis", "description": "Analyzes uploaded sample exams (Sonnet)"},
    {"feature": "quiz_generator", "label": "Quiz Generator", "description": "Generates quiz from learning asset + testing profile (Sonnet)"},
]


@router.get("/prompt-sockets", dependencies=[Depends(require_admin)])
async def get_prompt_sockets():
    return PROMPT_SOCKETS


@router.get("/prompts", dependencies=[Depends(require_admin)])
async def list_prompts(feature: str = None, framework_type: str = None, include_inactive: bool = False):
    sb = get_supabase()
    query = sb.table("base_prompts").select("*")
    if not include_inactive:
        query = query.eq("is_active", True)
    if feature:
        query = query.eq("feature", feature)
    if framework_type:
        query = query.eq("framework_type", framework_type)
    result = query.order("feature").order("version", desc=True).execute()
    return result.data


@router.post("/prompts", dependencies=[Depends(require_admin)])
async def create_prompt(request: Request):
    body = await request.json()
    feature = body.get("feature", "").strip()
    content = body.get("content", "").strip()
    framework_type = body.get("framework_type", "").strip() or None

    if not feature:
        raise HTTPException(status_code=400, detail="feature is required")
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    sb = get_supabase()

    existing_query = sb.table("base_prompts").select("version").eq("feature", feature).eq("is_active", True)
    if framework_type:
        existing_query = existing_query.eq("framework_type", framework_type)
    else:
        existing_query = existing_query.is_("framework_type", "null")
    existing = existing_query.order("version", desc=True).limit(1).execute()

    next_version = 1
    if existing.data:
        next_version = existing.data[0]["version"] + 1

    result = sb.table("base_prompts").insert({
        "feature": feature,
        "framework_type": framework_type,
        "content": content,
        "version": next_version,
        "is_active": True,
        "created_by": "admin",
    }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create prompt")

    return result.data[0]


@router.put("/prompts/{prompt_id}", dependencies=[Depends(require_admin)])
async def edit_prompt(prompt_id: str, request: Request):
    body = await request.json()
    new_content = body.get("content", "").strip()

    if not new_content:
        raise HTTPException(status_code=400, detail="content is required")

    sb = get_supabase()
    current = sb.table("base_prompts").select("*").eq("id", prompt_id).execute()
    if not current.data:
        raise HTTPException(status_code=404, detail="Prompt not found")

    old_prompt = current.data[0]
    sb.table("base_prompts").update({"is_active": False}).eq("id", prompt_id).execute()

    result = sb.table("base_prompts").insert({
        "feature": old_prompt["feature"],
        "framework_type": old_prompt["framework_type"],
        "content": new_content,
        "version": old_prompt["version"] + 1,
        "is_active": True,
        "created_by": "admin",
    }).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create new version")

    return result.data[0]


@router.get("/prompts/{prompt_id}/history", dependencies=[Depends(require_admin)])
async def prompt_history(prompt_id: str):
    sb = get_supabase()
    current = sb.table("base_prompts").select("feature, framework_type").eq("id", prompt_id).execute()
    if not current.data:
        raise HTTPException(status_code=404, detail="Prompt not found")

    prompt = current.data[0]
    query = sb.table("base_prompts").select("*").eq("feature", prompt["feature"])
    if prompt["framework_type"]:
        query = query.eq("framework_type", prompt["framework_type"])
    else:
        query = query.is_("framework_type", "null")
    result = query.order("version", desc=True).execute()
    return result.data


@router.post("/prompts/{prompt_id}/rollback", dependencies=[Depends(require_admin)])
async def rollback_prompt(prompt_id: str):
    sb = get_supabase()
    target = sb.table("base_prompts").select("*").eq("id", prompt_id).execute()
    if not target.data:
        raise HTTPException(status_code=404, detail="Prompt not found")

    target_prompt = target.data[0]

    deactivate_query = sb.table("base_prompts").select("id").eq("feature", target_prompt["feature"]).eq("is_active", True)
    if target_prompt["framework_type"]:
        deactivate_query = deactivate_query.eq("framework_type", target_prompt["framework_type"])
    else:
        deactivate_query = deactivate_query.is_("framework_type", "null")
    active_prompts = deactivate_query.execute()

    for p in active_prompts.data:
        sb.table("base_prompts").update({"is_active": False}).eq("id", p["id"]).execute()

    sb.table("base_prompts").update({"is_active": True}).eq("id", prompt_id).execute()
    return {"status": "rolled_back", "active_version": target_prompt["version"]}


@router.post("/prompts/global-replace", dependencies=[Depends(require_admin)])
async def global_replace(request: Request):
    body = await request.json()
    find_text = body.get("find", "")
    replace_text = body.get("replace", "")

    if not find_text:
        raise HTTPException(status_code=400, detail="find text is required")

    sb = get_supabase()
    active = sb.table("base_prompts").select("*").eq("is_active", True).execute()

    updated = []
    for prompt in active.data:
        if find_text in prompt["content"]:
            new_content = prompt["content"].replace(find_text, replace_text)
            sb.table("base_prompts").update({"is_active": False}).eq("id", prompt["id"]).execute()
            result = sb.table("base_prompts").insert({
                "feature": prompt["feature"],
                "framework_type": prompt["framework_type"],
                "content": new_content,
                "version": prompt["version"] + 1,
                "is_active": True,
                "created_by": "admin",
            }).execute()
            if result.data:
                updated.append({
                    "feature": prompt["feature"],
                    "old_version": prompt["version"],
                    "new_version": prompt["version"] + 1,
                })

    return {"updated": updated, "count": len(updated)}


# ── Batch Jobs ────────────────────────────────────────

@router.get("/batch-jobs", dependencies=[Depends(require_admin)])
async def list_batch_jobs():
    """List all batch jobs, most recent first."""
    sb = get_supabase()

    result = sb.table("batch_jobs").select(
        "id, topic_id, status, current_step, steps_completed, error_log, started_at, completed_at, created_at"
    ).order("created_at", desc=True).limit(50).execute()

    # Enrich with topic name
    jobs = result.data
    if jobs:
        topic_ids = list(set(j["topic_id"] for j in jobs))
        topics_result = sb.table("topics").select("id, name").in_("id", topic_ids).execute()
        topic_names = {t["id"]: t["name"] for t in topics_result.data}
        for job in jobs:
            job["topic_name"] = topic_names.get(job["topic_id"], "Unknown")

    return {"jobs": jobs}


@router.post("/topics/{topic_id}/rerun", dependencies=[Depends(require_admin)])
async def rerun_generation(topic_id: str):
    """Re-run the generation pipeline for a topic. Resets status and kicks off a new run."""
    import asyncio
    from app.services.pipeline import run_pipeline

    sb = get_supabase()

    # Verify topic exists
    topic_result = sb.table("topics").select("id, parsed_text_url").eq("id", topic_id).execute()
    if not topic_result.data:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic = topic_result.data[0]
    if not topic.get("parsed_text_url"):
        raise HTTPException(status_code=400, detail="No parsed text found — upload files first")

    # Reset topic status and all output URLs
    sb.table("topics").update({
        "generation_status": "pending",
        "learning_asset_url": None,
        "podcast_script_url": None,
        "podcast_audio_url": None,
        "notechart_url": None,
        "visual_overview_script_url": None,
        "visual_overview_images": [],
        "visual_overview_audio_urls": [],
    }).eq("id", topic_id).execute()

    # Kick off pipeline in background
    asyncio.create_task(run_pipeline(topic_id, sb))

    return {"status": "started", "topic_id": topic_id, "message": "Generation pipeline re-started"}


# ── Activity ──────────────────────────────────────────

@router.get("/activity", dependencies=[Depends(require_admin)])
async def get_activity():
    sb = get_supabase()

    # Get recent batch jobs
    jobs = sb.table("batch_jobs").select("*, topics(name, course_id)").order("created_at", desc=True).limit(50).execute()

    # Get recent topics created
    topics = sb.table("topics").select("*, courses(name, student_id)").order("created_at", desc=True).limit(50).execute()

    # Get student count and course count
    students = sb.table("students").select("id", count="exact").is_("archived_at", "null").execute()
    courses = sb.table("courses").select("id", count="exact").eq("active", True).execute()
    topic_count = sb.table("topics").select("id", count="exact").execute()

    return {
        "recent_jobs": jobs.data,
        "recent_topics": topics.data,
        "stats": {
            "students": students.count or 0,
            "courses": courses.count or 0,
            "topics": topic_count.count or 0,
        },
    }
