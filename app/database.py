from datetime import datetime
from sqlalchemy import String, DateTime, Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
import enum

from app.config import settings

engine = create_async_engine(settings.DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class VideoStatus(str, enum.Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    READY      = "ready"
    FAILED     = "failed"


class Video(Base):
    __tablename__ = "videos"

    id:           Mapped[str] = mapped_column(String, primary_key=True)
    title:        Mapped[str] = mapped_column(String)
    original_key: Mapped[str] = mapped_column(String)           # S3 key in videos bucket
    hls_key:      Mapped[str] = mapped_column(String, nullable=True)  # S3 prefix in hls bucket
    status:       Mapped[VideoStatus] = mapped_column(
                      SAEnum(VideoStatus), default=VideoStatus.PENDING)
    error:        Mapped[str] = mapped_column(String, nullable=True)
    created_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at:   Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow,
                                                   onupdate=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    id:              Mapped[str] = mapped_column(String, primary_key=True)
    username:        Mapped[str] = mapped_column(String, unique=True)
    hashed_password: Mapped[str] = mapped_column(String)
    created_at:      Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def create_tables():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
