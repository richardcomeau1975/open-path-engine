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


def delete_from_r2(key: str) -> bool:
    """Delete a single object from R2. Returns True if successful."""
    r2 = get_r2_client()
    try:
        r2.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=key)
        return True
    except Exception:
        return False


def delete_r2_prefix(prefix: str) -> int:
    """Delete all objects under a given prefix from R2. Returns count deleted."""
    r2 = get_r2_client()
    count = 0
    try:
        response = r2.list_objects_v2(Bucket=settings.R2_BUCKET_NAME, Prefix=prefix)
        objects = response.get("Contents", [])
        for obj in objects:
            r2.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=obj["Key"])
            count += 1
        while response.get("IsTruncated"):
            response = r2.list_objects_v2(
                Bucket=settings.R2_BUCKET_NAME,
                Prefix=prefix,
                ContinuationToken=response["NextContinuationToken"],
            )
            for obj in response.get("Contents", []):
                r2.delete_object(Bucket=settings.R2_BUCKET_NAME, Key=obj["Key"])
                count += 1
    except Exception:
        pass
    return count
