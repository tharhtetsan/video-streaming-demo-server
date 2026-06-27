# 🎬 HLS Streaming Server — Production Demo

A production-ready video streaming stack built with **FastAPI**, **Celery**, **PostgreSQL**, **MinIO (S3)**, and **Nginx as a CDN cache layer**.

---

## Architecture

```
Browser
   │
   ▼
┌─────────────────────────────────────────────────────┐
│  Nginx  (:80)  — Reverse proxy + HLS CDN cache      │
│   /          → FastAPI (API + player UI)             │
│   /hls/      → MinIO hls bucket  ← cached here ✓    │
└─────────────────────────────────────────────────────┘
        │                        │
        ▼                        ▼
┌──────────────┐        ┌───────────────┐
│  FastAPI     │        │  MinIO        │
│  :8000       │        │  :9000        │
│              │        │  videos/      │  ← raw uploads
│  4 workers   │        │  hls/         │  ← segments (public)
│  (Gunicorn)  │        └───────────────┘
└──────────────┘
        │
        ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  PostgreSQL  │    │  Redis       │    │  Celery      │
│  :5432       │    │  :6379       │    │  Worker      │
│              │    │  (broker)    │    │              │
│  videos      │    └──────────────┘    │  FFmpeg      │
│  users       │           │            │  multi-bitrate│
└──────────────┘           └──────────→│  HLS conv.   │
                                        └──────────────┘
```

---

## Services

| Service | Port | Purpose |
|---------|------|---------|
| Nginx | 80 | Reverse proxy + HLS CDN cache |
| FastAPI | (internal) | API + player UI |
| Celery Worker | — | FFmpeg HLS conversion |
| PostgreSQL | (internal) | Video metadata + users |
| Redis | (internal) | Celery job broker |
| MinIO | (internal) | S3-compatible object storage |
| MinIO Console | 9001 | Storage browser UI |
| Flower | 5555 | Celery job monitor |

---

## Quick Start

### 1. Start the stack

```bash
docker compose up --build
```

### 2. Open the dashboard

```
http://localhost
```

Register an account, upload a video, and watch it stream.

---

## How It Works

### Upload flow

```
POST /videos/upload
  → stream file directly to MinIO (videos bucket)
  → create Video row in PostgreSQL (status: pending)
  → dispatch Celery task
  → return { video_id, status_url }
```

### Processing flow (Celery Worker)

```
convert_to_hls task
  → download raw video from MinIO to /tmp
  → FFmpeg: produce 3-bitrate HLS ladder
      480p  800k  ─┐
      720p  1400k  ─┼─ master.m3u8
      1080p 2800k  ─┘
  → upload all .m3u8 + .ts files to MinIO (hls bucket)
  → update Video status → ready in PostgreSQL
  → clean up /tmp scratch space
```

### Streaming flow

```
GET /player/{video_id}?token=<jwt>
  → verify short-lived stream token (JWT scoped to video_id)
  → render player with playlist_url = /hls/{video_id}/master.m3u8

Browser (hls.js)
  → GET /hls/{video_id}/master.m3u8
       → Nginx checks cache (MISS first time, HIT after)
       → Nginx proxies to MinIO hls bucket
  → hls.js picks best quality variant based on bandwidth
  → fetches .ts segments through same Nginx cache
```

---

## CDN / Nginx Cache

Nginx acts as an edge cache for HLS delivery:

- `.ts` segments are **immutable** — cached for 1 hour
- `.m3u8` playlists — short TTL (5s) for live stream compatibility
- `X-Cache-Status` response header shows `HIT` / `MISS` / `BYPASS`
- Supports HTTP Range requests for segment seeking
- In production, replace Nginx with **CloudFront**, **Cloudflare**, or **BunnyCDN** pointing at your MinIO/S3 bucket

---

## Auth

- Users register and login via `/auth/register` and `/auth/login`
- API routes are protected by **JWT Bearer tokens** (4-hour expiry)
- Video player URLs use a **short-lived stream token** scoped to a single video
- Passwords are hashed with **bcrypt**

---

## API Reference

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| POST | `/auth/register` | — | Create account |
| POST | `/auth/login` | — | Get JWT token |
| POST | `/videos/upload` | JWT | Upload video → queues Celery job |
| GET | `/videos` | JWT | List all videos with status |
| GET | `/videos/{id}/status` | JWT | Poll processing status |
| GET | `/player/{id}?token=` | Stream token | HTML video player |
| GET | `/hls/{id}/master.m3u8` | Public | HLS master playlist (via Nginx) |

---

## Monitoring

| URL | Tool |
|-----|------|
| http://localhost:5555 | Flower — Celery job dashboard |
| http://localhost:9001 | MinIO Console — browse buckets |

---


## Moving to Real Production

| Component | Demo | Production swap |
|-----------|------|-----------------|
| MinIO | Local Docker | AWS S3 / Cloudflare R2 |
| Nginx cache | Single node | CloudFront / Cloudflare CDN |
| JWT secret | Env var | AWS Secrets Manager / Vault |
| Celery workers | 1 container | Auto-scaling worker pool (ECS/K8s) |
| PostgreSQL | Docker volume | RDS / Cloud SQL |
| Redis | Docker | ElastiCache / Upstash |


## Video Quality work
```bash
output/
├── master.m3u8          ← index of all qualities
├── stream_0/
│   ├── playlist.m3u8    ← 480p index
│   ├── seg000.ts
│   └── seg001.ts ...
├── stream_1/
│   ├── playlist.m3u8    ← 720p index
│   └── seg000.ts ...
└── stream_2/
    ├── playlist.m3u8    ← 1080p index
    └── seg000.ts ...
```
switches quality automatically:
```bash
Buffer > 30s + fast connection  → switch UP   to higher quality
Buffer < 10s + slow connection  → switch DOWN to lower quality
```

```bash
FFmpeg          →  3 quality streams in MinIO
master.m3u8     →  hls.js discovers all variants
ABR algorithm   →  auto-picks best quality per network
hls.currentLevel →  manual override from dropdown
Segment boundary →  seamless quality switch mid-playback
```