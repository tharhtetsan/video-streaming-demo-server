from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str
    REDIS_URL: str

    S3_ENDPOINT: str
    S3_ACCESS_KEY: str
    S3_SECRET_KEY: str
    S3_VIDEO_BUCKET: str = "videos"
    S3_HLS_BUCKET: str = "hls"

    JWT_SECRET: str
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = 4

    CDN_BASE_URL: str  # e.g. http://localhost/hls

    class Config:
        env_file = ".env"


settings = Settings()
