import aioboto3
from botocore.config import Config
from app.config import settings

_session = aioboto3.Session(
    aws_access_key_id=settings.S3_ACCESS_KEY,
    aws_secret_access_key=settings.S3_SECRET_KEY,
)

_s3_config = Config(signature_version="s3v4")


def s3_client():
    """Async context manager for S3 client."""
    return _session.client(
        "s3",
        endpoint_url=settings.S3_ENDPOINT,
        config=_s3_config,
    )


async def upload_fileobj(fileobj, bucket: str, key: str, content_type: str = "application/octet-stream"):
    async with s3_client() as s3:
        await s3.upload_fileobj(
            fileobj, bucket, key,
            ExtraArgs={"ContentType": content_type}
        )


async def generate_presigned_url(bucket: str, key: str, expires: int = 3600) -> str:
    async with s3_client() as s3:
        return await s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires,
        )


def public_hls_url(video_id: str, filename: str) -> str:
    """
    Returns the Nginx-cached CDN URL for an HLS file.
    Nginx proxies /hls/ → MinIO hls bucket, with caching.
    """
    return f"{settings.CDN_BASE_URL}/{video_id}/{filename}"
