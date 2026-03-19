from fastapi import APIRouter, Depends, HTTPException
from app.middleware.clerk_auth import get_current_student
from app.services.supabase import get_supabase

router = APIRouter(prefix="/api", tags=["topics"])


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
        # In Phase 0, nothing is generated yet
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
