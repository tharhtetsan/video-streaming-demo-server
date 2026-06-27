import os
import asyncio
import subprocess
import shutil
import logging
from pathlib import Path

import boto3
from botocore.config import Config
from celery import Celery
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

# Sync versions for Celery (Celery doesn't support asyncio natively)
from app.config import settings
from app.database import Video, VideoStatus

logger = logging.getLogger(__name__)

celery = Celery(
    "worker",
    broker=os.environ["REDIS_URL"],
    backend=os.environ["REDIS_URL"],
)

celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    worker_prefetch_multiplier=1,   # one task at a time per worker (FFmpeg is CPU-heavy)
)

# Sync SQLAlchemy engine for Celery tasks
sync_engine = create_engine(
    settings.DATABASE_URL.replace("+asyncpg", "+psycopg2"),
    pool_pre_ping=True,
)

# Sync S3 client
s3 = boto3.client(
    "s3",
    endpoint_url=settings.S3_ENDPOINT,
    aws_access_key_id=settings.S3_ACCESS_KEY,
    aws_secret_access_key=settings.S3_SECRET_KEY,
    config=Config(signature_version="s3v4"),
)


def set_video_status(video_id: str, status: VideoStatus, error: str = None):
    with Session(sync_engine) as session:
        session.execute(
            update(Video)
            .where(Video.id == video_id)
            .values(status=status, error=error)
        )
        session.commit()


def upload_directory_to_s3(local_dir: str, bucket: str, prefix: str):
    """Upload all HLS files from local_dir to S3 under prefix/."""
    for root, _, files in os.walk(local_dir):
        for filename in files:
            local_path = os.path.join(root, filename)
            relative = os.path.relpath(local_path, local_dir)
            s3_key = f"{prefix}/{relative}"

            if filename.endswith(".m3u8"):
                content_type = "application/vnd.apple.mpegurl"
            elif filename.endswith(".ts"):
                content_type = "video/mp2t"
            else:
                content_type = "application/octet-stream"

            s3.upload_file(
                local_path, bucket, s3_key,
                ExtraArgs={"ContentType": content_type},
            )
            logger.info(f"Uploaded {s3_key}")


@celery.task(bind=True, max_retries=3, default_retry_delay=30)
def convert_to_hls(self, video_id: str, s3_key: str):
    """
    1. Download raw video from MinIO
    2. FFmpeg → multi-bitrate HLS (480p, 720p, 1080p + master playlist)
    3. Upload HLS segments back to MinIO hls bucket
    4. Update video status in PostgreSQL
    """
    work_dir = f"/tmp/hls_work/{video_id}"
    input_path = f"{work_dir}/input.mp4"
    output_dir = f"{work_dir}/output"

    try:
        set_video_status(video_id, VideoStatus.PROCESSING)

        # ── Step 1: Download from MinIO ───────────────────────────
        os.makedirs(work_dir, exist_ok=True)
        logger.info(f"[{video_id}] Downloading {s3_key}")
        s3.download_file(settings.S3_VIDEO_BUCKET, s3_key, input_path)

        # ── Step 2: FFmpeg multi-bitrate HLS ─────────────────────
        os.makedirs(output_dir, exist_ok=True)

        # Adaptive bitrate ladder: 480p / 720p / 1080p
        # Each variant gets its own .m3u8 + .ts segments
        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,

            # 480p stream
            "-filter_complex", "[0:v]split=3[v1][v2][v3]",
            "-map", "[v1]", "-map", "0:a",
            "-b:v:0", "800k", "-maxrate:v:0", "856k", "-bufsize:v:0", "1200k",
            "-s:v:0", "854x480",

            # 720p stream
            "-map", "[v2]", "-map", "0:a",
            "-b:v:1", "1400k", "-maxrate:v:1", "1498k", "-bufsize:v:1", "2100k",
            "-s:v:1", "1280x720",

            # 1080p stream
            "-map", "[v3]", "-map", "0:a",
            "-b:v:2", "2800k", "-maxrate:v:2", "2996k", "-bufsize:v:2", "4200k",
            "-s:v:2", "1920x1080",

            # Audio codec for all streams
            "-c:a", "aac", "-ar", "48000",
            "-c:v", "libx264", "-crf", "20",

            # HLS output settings
            "-f", "hls",
            "-hls_time", "6",
            "-hls_playlist_type", "vod",
            "-hls_flags", "independent_segments",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", f"{output_dir}/stream_%v/seg%03d.ts",
            "-master_pl_name", "master.m3u8",

            # Output per-variant playlists
            "-var_stream_map", "v:0,a:0 v:1,a:1 v:2,a:2",
            f"{output_dir}/stream_%v/playlist.m3u8",
        ]

        logger.info(f"[{video_id}] Running FFmpeg")
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr[-500:]}")

        # ── Step 3: Upload HLS output to MinIO ────────────────────
        logger.info(f"[{video_id}] Uploading HLS to MinIO")
        upload_directory_to_s3(output_dir, settings.S3_HLS_BUCKET, video_id)

        # ── Step 4: Mark as READY in PostgreSQL ───────────────────
        with Session(sync_engine) as session:
            session.execute(
                update(Video)
                .where(Video.id == video_id)
                .values(
                    status=VideoStatus.READY,
                    hls_key=f"{video_id}/master.m3u8",
                )
            )
            session.commit()

        logger.info(f"[{video_id}] Done ✓")

    except Exception as exc:
        logger.error(f"[{video_id}] Failed: {exc}")
        set_video_status(video_id, VideoStatus.FAILED, error=str(exc)[:500])
        raise self.retry(exc=exc)

    finally:
        # Always clean up scratch space
        shutil.rmtree(work_dir, ignore_errors=True)
