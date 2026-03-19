import jwt
import httpx
from fastapi import Depends, HTTPException, Request
from app.config import settings

_jwks_cache = None


async def _get_jwks():
    global _jwks_cache
    if _jwks_cache is None:
        jwks_url = f"https://{_get_clerk_domain()}/.well-known/jwks.json"
        async with httpx.AsyncClient() as client:
            resp = await client.get(jwks_url)
            resp.raise_for_status()
            _jwks_cache = resp.json()
    return _jwks_cache

def _get_clerk_domain():
    # Extract domain from the publishable key
    # pk_test_xxxxx where xxxxx is base64 of the domain
    import base64
    key = settings.CLERK_PUBLISHABLE_KEY
    # Remove pk_test_ or pk_live_ prefix
    encoded = key.split("_")[-1]
    # Add padding if needed
    padding = 4 - len(encoded) % 4
    if padding != 4:
        encoded += "=" * padding
    domain = base64.b64decode(encoded).decode("utf-8").rstrip("$")
    return domain


async def get_current_clerk_user_id(request: Request) -> str:
    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid authorization header")

    token = auth_header.split(" ", 1)[1]

    try:
        jwks = await _get_jwks()
        # Get the signing key
        unverified_header = jwt.get_unverified_header(token)
        kid = unverified_header.get("kid")

        signing_key = None
        for key in jwks.get("keys", []):
            if key.get("kid") == kid:
                signing_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
                break

        if signing_key is None:
            raise HTTPException(status_code=401, detail="Unable to find signing key")

        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )

        clerk_user_id = payload.get("sub")
        if not clerk_user_id:
            raise HTTPException(status_code=401, detail="No user ID in token")

        return clerk_user_id

    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"Invalid token: {str(e)}")


async def get_current_student(clerk_user_id: str = Depends(get_current_clerk_user_id)):
    from app.services.supabase import get_supabase
    sb = get_supabase()
    result = sb.table("students").select("*").eq("clerk_id", clerk_user_id).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="Student not found")

    return result.data[0]
