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


def upload_text_to_r2(r2_client, key: str, text: str) -> str:
    """Upload a text string to R2. Returns the R2 key."""
    r2_client.put_object(
        Bucket=settings.R2_BUCKET_NAME,
        Key=key,
        Body=text.encode("utf-8"),
        ContentType="text/plain",
    )
    return key
