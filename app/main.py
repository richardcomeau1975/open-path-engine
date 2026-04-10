import logging

logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers.students import router as students_router
from app.routers.webhooks import router as webhooks_router
from app.routers.courses import router as courses_router
from app.routers.topics import router as topics_router
from app.routers.admin import router as admin_router
from app.routers.generate import router as generate_router
from app.routers.content import router as content_router
from app.routers.walkthrough import router as walkthrough_router
from app.routers.voice import router as voice_router
from app.routers.topic_admin import router as topic_admin_router
from app.routers.travel import router as travel_router
from app.routers.travel_realtime import router as travel_realtime_router

app = FastAPI(title="Open Path Engine", version="0.1.0")

# CORS
origins = settings.get_allowed_origins()
if origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Routers
app.include_router(students_router)
app.include_router(webhooks_router)
app.include_router(courses_router)
app.include_router(topics_router)
app.include_router(admin_router)
app.include_router(generate_router)
app.include_router(content_router)
app.include_router(walkthrough_router)
app.include_router(voice_router)
app.include_router(topic_admin_router)
app.include_router(travel_router)
app.include_router(travel_realtime_router)


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/api/health/services")
async def health_services():
    results = {}

    # Check Supabase
    try:
        from app.services.supabase import get_supabase
        sb = get_supabase()
        sb.table("students").select("id").limit(1).execute()
        results["supabase"] = "ok"
    except Exception as e:
        results["supabase"] = f"error: {str(e)}"

    # Check R2
    try:
        from app.services.r2 import get_r2_client
        r2 = get_r2_client()
        r2.head_bucket(Bucket=settings.R2_BUCKET_NAME)
        results["r2"] = "ok"
    except Exception as e:
        results["r2"] = f"error: {str(e)}"

    return results
