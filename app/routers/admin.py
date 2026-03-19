import secrets
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from app.config import settings
from app.services.supabase import get_supabase

router = APIRouter(prefix="/api/admin", tags=["admin"])

# Simple token store (in-memory, resets on restart — fine for single admin)
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

    # Check if student already exists by email
    if email:
        existing = sb.table("students").select("id").eq("email", email).execute()
        if existing.data:
            raise HTTPException(status_code=409, detail="Student with this email already exists")

    # Split name into first/last for Clerk
    name_parts = name.split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    # Create user in Clerk via Backend API
    clerk_payload = {
        "first_name": first_name,
        "last_name": last_name,
    }
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
                error_detail = resp.text
                raise HTTPException(
                    status_code=502,
                    detail=f"Clerk API error: {error_detail}",
                )
            clerk_user = resp.json()
            clerk_id = clerk_user["id"]
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Failed to reach Clerk: {str(e)}")

    student_data = {
        "clerk_id": clerk_id,
        "name": name,
        "email": email or None,
    }
    result = sb.table("students").insert(student_data).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create student in database")

    return result.data[0]


# ── Courses ───────────────────────────────────────────

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


@router.get("/courses", dependencies=[Depends(require_admin)])
async def list_courses():
    sb = get_supabase()
    result = sb.table("courses").select("*, students(name, email)").order("created_at", desc=True).execute()
    return result.data


# ── Prompts ───────────────────────────────────────────

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

    # Check for existing active prompt with same feature + framework_type
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

    # Get the current prompt
    current = sb.table("base_prompts").select("*").eq("id", prompt_id).execute()
    if not current.data:
        raise HTTPException(status_code=404, detail="Prompt not found")

    old_prompt = current.data[0]

    # Deactivate the old version
    sb.table("base_prompts").update({"is_active": False}).eq("id", prompt_id).execute()

    # Create new version
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

    # Get this prompt to find its feature + framework_type
    current = sb.table("base_prompts").select("feature, framework_type").eq("id", prompt_id).execute()
    if not current.data:
        raise HTTPException(status_code=404, detail="Prompt not found")

    prompt = current.data[0]

    # Get all versions for this feature + framework_type
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

    # Get the prompt to rollback to
    target = sb.table("base_prompts").select("*").eq("id", prompt_id).execute()
    if not target.data:
        raise HTTPException(status_code=404, detail="Prompt not found")

    target_prompt = target.data[0]

    # Deactivate all active versions for this feature + framework_type
    deactivate_query = sb.table("base_prompts").select("id").eq("feature", target_prompt["feature"]).eq("is_active", True)
    if target_prompt["framework_type"]:
        deactivate_query = deactivate_query.eq("framework_type", target_prompt["framework_type"])
    else:
        deactivate_query = deactivate_query.is_("framework_type", "null")

    active_prompts = deactivate_query.execute()
    for p in active_prompts.data:
        sb.table("base_prompts").update({"is_active": False}).eq("id", p["id"]).execute()

    # Activate the target version
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

    # Get all active prompts
    active = sb.table("base_prompts").select("*").eq("is_active", True).execute()

    updated = []
    for prompt in active.data:
        if find_text in prompt["content"]:
            new_content = prompt["content"].replace(find_text, replace_text)

            # Deactivate old version
            sb.table("base_prompts").update({"is_active": False}).eq("id", prompt["id"]).execute()

            # Create new version
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
                    "framework_type": prompt["framework_type"],
                    "old_version": prompt["version"],
                    "new_version": prompt["version"] + 1,
                })

    return {"updated": updated, "count": len(updated)}
