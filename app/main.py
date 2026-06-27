import uuid
import re
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.database import get_db, create_tables, Video, VideoStatus, User
from app.auth import (
    hash_password, verify_password, create_access_token,
    create_stream_token, verify_stream_token, get_current_user,
)
from app.storage import upload_fileobj, public_hls_url




# ── Startup ───────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_tables()
    yield
app = FastAPI(title="HLS Streaming Server",lifespan=lifespan)


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

templates = Jinja2Templates(directory="templates")



# ── Auth routes ───────────────────────────────────────────────────────────────

@app.post("/auth/register", status_code=201)
async def register(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(User).where(User.username == form.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Username already taken")

    user = User(
        id=str(uuid.uuid4()),
        username=form.username,
        hashed_password=hash_password(form.password),
    )
    db.add(user)
    await db.commit()
    return {"message": "User created", "username": user.username}


@app.post("/auth/login")
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == form.username))
    user = result.scalar_one_or_none()

    if not user or not verify_password(form.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    return {
        "access_token": create_access_token(user.id),
        "token_type": "bearer",
    }


# ── Upload route ──────────────────────────────────────────────────────────────

def safe_name(filename: str) -> str:
    """Sanitize filename: strip path traversal, keep only safe chars."""
    stem = Path(filename).stem
    return re.sub(r"[^a-zA-Z0-9_-]", "_", stem)[:64]


@app.post("/videos/upload", status_code=202)
async def upload_video(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Validate MIME type
    allowed = {"video/mp4", "video/quicktime", "video/x-msvideo", "video/x-matroska"}
    if file.content_type not in allowed:
        raise HTTPException(status_code=415, detail=f"Unsupported file type: {file.content_type}")

    video_id = str(uuid.uuid4())
    s3_key = f"{video_id}/{safe_name(file.filename)}{Path(file.filename).suffix}"

    # Stream upload directly to MinIO — never loads full file into RAM
    await upload_fileobj(file.file, settings.S3_VIDEO_BUCKET, s3_key, file.content_type)

    video = Video(
        id=video_id,
        title=safe_name(file.filename),
        original_key=s3_key,
        status=VideoStatus.PENDING,
    )
    db.add(video)
    await db.commit()

    # Dispatch Celery job
    from celery import Celery
    celery_app = Celery(broker=settings.REDIS_URL)

    # Then in upload_video():
    celery_app.send_task(
        "worker.tasks.convert_to_hls",
        args=[video_id, s3_key],
    )

    return {
        "video_id": video_id,
        "status": "processing",
        "status_url": f"/videos/{video_id}/status",
    }


# ── Video status ──────────────────────────────────────────────────────────────

@app.get("/videos/{video_id}/status")
async def video_status(
    video_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    response = {
        "video_id": video.id,
        "title": video.title,
        "status": video.status,
        "created_at": video.created_at,
    }

    if video.status == VideoStatus.READY:
        stream_token = create_stream_token(video_id)
        response["player_url"] = f"/player/{video_id}?token={stream_token}"

    if video.status == VideoStatus.FAILED:
        response["error"] = video.error

    return response


# ── List videos ───────────────────────────────────────────────────────────────

@app.get("/videos")
async def list_videos(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await db.execute(
        select(Video).order_by(Video.created_at.desc()).limit(50)
    )
    videos = result.scalars().all()

    items = []
    for v in videos:
        item = {
            "video_id": v.id,
            "title": v.title,
            "status": v.status,
            "created_at": v.created_at,
        }
        if v.status == VideoStatus.READY:
            token = create_stream_token(v.id)
            item["player_url"] = f"/player/{v.id}?token={token}"
        items.append(item)

    return items


# ── Player UI ─────────────────────────────────────────────────────────────────
@app.get("/player/{video_id}", response_class=HTMLResponse)
async def player(
    request: Request,
    video_id: str,
    token: str = None,
    db: AsyncSession = Depends(get_db),
):
    if not token or not verify_stream_token(token, video_id):
        raise HTTPException(status_code=403, detail="Invalid or expired stream token")

    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalar_one_or_none()
    if not video or video.status != VideoStatus.READY:
        raise HTTPException(status_code=404, detail="Video not ready")

    playlist_url = public_hls_url(video_id, "master.m3u8")

    # ← new signature: request first, then name, then context (no "request" in dict)
    return templates.TemplateResponse(request, "video_player.html", {
        "video": video,
        "playlist_url": playlist_url,
    })


# ── Dashboard UI ──────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    # ← new signature: request first, then name, then context
    return templates.TemplateResponse(request, "dashboard.html", {})