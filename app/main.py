from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.routers.students import router as students_router

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
