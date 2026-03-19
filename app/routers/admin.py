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


@router.post("/login")
async def admin_login(request: Request):
    body = await request.json()
    password = body.get("password", "")
    if password != settings.ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong password")
    token = secrets.token_urlsafe(32)
    _admin_tokens.add(token)
    return {"token": token}


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
        # Ensure phone has + prefix for E.164 format
        if not phone.startswith("+"):
            phone = f"+1{phone}"  # Default to US/Canada
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

    # Create student in Supabase with clerk_id
    student_data = {
        "clerk_id": clerk_id,
        "name": name,
        "email": email or None,
    }
    result = sb.table("students").insert(student_data).execute()

    if not result.data:
        raise HTTPException(status_code=500, detail="Failed to create student in database")

    return result.data[0]


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

    # Verify student exists
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
