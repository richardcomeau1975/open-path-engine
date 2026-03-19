from fastapi import APIRouter, Depends
from app.middleware.clerk_auth import get_current_student

router = APIRouter(prefix="/api", tags=["students"])


@router.get("/me")
async def get_me(student: dict = Depends(get_current_student)):
    return student
