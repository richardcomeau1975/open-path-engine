from fastapi import APIRouter, Depends, HTTPException
from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase

router = APIRouter(prefix="/api", tags=["courses"])


@router.get("/courses")
async def list_courses(student: dict = Depends(get_current_student)):
    sb = get_supabase()
    result = sb.table("courses").select("*").eq("student_id", student["id"]).eq("active", True).order("created_at").execute()
    return result.data
