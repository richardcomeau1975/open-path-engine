import boto3
from app.config import settings


def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def upload_text_to_r2(key: str, text: str) -> str:
    """Upload a text string to R2. Returns the R2 key."""
    r2 = get_r2_client()
    r2.put_object(
        Bucket=settings.R2_BUCKET_NAME,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain",
    )
    return key


def download_from_r2(key: str) -> bytes:
    """Download a file from R2 and return its bytes."""
    r2 = get_r2_client()
    response = r2.get_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
    return response["Body"].read()


def upload_bytes_to_r2(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """Upload raw bytes to R2. Returns the key."""
    r2 = get_r2_client()
    r2.put_object(
        Bucket=settings.R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return key


def generate_presigned_url(key: str, expires_in: int = 3600) -> str:
    """
    Generate a presigned URL for an R2 object.
    Default expiry: 1 hour.
    """
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.R2_BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )
    return url


def generate_presigned_urls(keys: list[str], expires_in: int = 3600) -> dict[str, str]:
    """
    Generate presigned URLs for multiple R2 objects.
    Returns a dict of {key: presigned_url}.
    Skips keys that are None or empty.
    """
    result = {}
    for key in keys:
        if key:
            result[key] = generate_presigned_url(key, expires_in)
    return result
