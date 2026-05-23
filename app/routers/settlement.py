"""
Settlement domain routes.

Phase 2: the generator endpoint. Takes a situation and the client's stated need,
produces the dot-based intermediate representation, stores it on R2, returns it.
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from app.middleware.clerk_auth import get_current_student
from app.services.settlement_generator import generate_settlement_asset
from app.services.r2 import upload_text_to_r2

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settlement", tags=["settlement"])


@router.post("/generate")
async def settlement_generate(request: Request, student: dict = Depends(get_current_student)):
    """Run the settlement generator for a situation."""
    body = await request.json()
    situation_text = (body.get("situation_text") or "").strip()
    client_need = (body.get("client_need") or "").strip()

    if not situation_text:
        raise HTTPException(status_code=400, detail="situation_text is required")
    if not client_need:
        raise HTTPException(status_code=400, detail="client_need is required")

    try:
        asset = await asyncio.to_thread(
            generate_settlement_asset, situation_text, client_need
        )
    except json.JSONDecodeError as e:
        logger.error(f"Settlement generator returned invalid JSON: {e}")
        raise HTTPException(status_code=502, detail="Generator did not return valid JSON")
    except Exception as e:
        logger.error(f"Settlement generation failed: {e}")
        raise HTTPException(status_code=502, detail="Settlement generation failed")

    asset_id = str(uuid.uuid4())
    r2_key = f"settlement/{student['id']}/{asset_id}.json"
    try:
        upload_text_to_r2(r2_key, json.dumps(asset))
    except Exception as e:
        logger.error(f"Could not store settlement asset on R2: {e}")
        r2_key = None

    return {"asset_id": asset_id, "r2_key": r2_key, "asset": asset}
