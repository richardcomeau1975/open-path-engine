import json
from fastapi import APIRouter, Request, HTTPException
from svix.webhooks import Webhook, WebhookVerificationError
from app.config import settings
from app.services.supabase import get_supabase

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/clerk")
async def clerk_webhook(request: Request):
    body = await request.body()
    headers = dict(request.headers)

    # Verify the webhook signature
    try:
        wh = Webhook(settings.CLERK_WEBHOOK_SECRET)
        payload = wh.verify(body, headers)
    except WebhookVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    event_type = payload.get("type")

    if event_type == "user.created":
        data = payload.get("data", {})
        clerk_id = data.get("id")
        first_name = data.get("first_name", "")
        last_name = data.get("last_name", "")
        name = f"{first_name} {last_name}".strip() or "Unknown"

        # Get primary email
        email = None
        email_addresses = data.get("email_addresses", [])
        if email_addresses:
            email = email_addresses[0].get("email_address")

        sb = get_supabase()

        # Check if student already exists (admin may have created them first)
        existing = sb.table("students").select("id").eq("clerk_id", clerk_id).execute()
        if existing.data:
            return {"status": "already_exists"}

        # Also check by email in case admin created without clerk_id
        if email:
            existing_by_email = sb.table("students").select("id, clerk_id").eq("email", email).execute()
            if existing_by_email.data:
                student = existing_by_email.data[0]
                if not student.get("clerk_id"):
                    # Admin created this student, now fill in the clerk_id
                    sb.table("students").update({"clerk_id": clerk_id}).eq("id", student["id"]).execute()
                    return {"status": "linked"}
                return {"status": "already_exists"}

        # Create new student
        sb.table("students").insert({
            "clerk_id": clerk_id,
            "name": name,
            "email": email,
        }).execute()

        return {"status": "created"}

    return {"status": "ignored", "event": event_type}
