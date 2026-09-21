from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    username_normalized: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


class ChatThread(Base):
    __tablename__ = "chat_threads"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LongTermMemory(Base):
    __tablename__ = "long_term_memories"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "category",
            "topic",
            name="uq_long_term_memories_user_category_topic",
        ),
        Index(
            "ix_long_term_memories_pending_feedback",
            "user_id",
            "feedback_status",
            "expires_at",
        ),
        {"comment": "用户跨聊天线程共享的长期记忆"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="长期记忆主键"
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False, comment="所属用户 ID"
    )
    category: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="记忆类别：当前目标、稳定偏好或推荐"
    )
    topic: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="用于合并同类记忆的规范化主题"
    )
    content: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="提供给聊天模型的记忆内容"
    )
    status: Mapped[str] = mapped_column(
        String(20), index=True, nullable=False, comment="记忆状态：生效或已完成"
    )
    source_thread_id: Mapped[str | None] = mapped_column(
        ForeignKey("chat_threads.id"), nullable=True, comment="最近写入该记忆的聊天线程 ID"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, index=True, nullable=False, comment="最后更新时间（UTC）"
    )
    last_confirmed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="用户最后确认该记忆的时间（UTC）"
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime, index=True, nullable=True, comment="失效时间；空值表示长期有效"
    )
    feedback_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="推荐反馈状态：等待反馈或已过期"
    )
    details: Mapped[dict | None] = mapped_column(
        JSON, nullable=True, comment="推荐对象、理由及特征等结构化信息"
    )


class ApplicationState(Base):
    __tablename__ = "application_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    legacy_claimed_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )


class StudySession(Base):
    """一条从开始学习到停止学习的计时记录。"""

    __tablename__ = "study_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=True
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Hobby(Base):
    __tablename__ = "hobbies"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_hobbies_user_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PlanPreference(Base):
    __tablename__ = "plan_preferences"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    city_query: Mapped[str] = mapped_column(String(100), nullable=False)
    city_name: Mapped[str] = mapped_column(String(100), nullable=False)
    admin1: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    adcode: Mapped[str | None] = mapped_column(String(20), nullable=True)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    timezone: Mapped[str] = mapped_column(String(50), nullable=False, default="Asia/Shanghai")
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class DailyPlan(Base):
    __tablename__ = "daily_plans"
    __table_args__ = (UniqueConstraint("user_id", "plan_date", name="uq_daily_plans_user_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True, nullable=False)
    plan_date: Mapped[date] = mapped_column(Date, nullable=False)
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class LibraryBook(Base):
    __tablename__ = "library_books"
    __table_args__ = (
        UniqueConstraint("user_id", "sha256", name="uq_library_books_user_sha256"),
        Index("ix_library_books_user_status", "user_id", "status"),
        {"comment": "用户上传并用于 RAG 检索的电子书"},
    )

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="书籍主键"
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False, comment="所属用户 ID"
    )
    title: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="书籍显示标题"
    )
    author: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="从电子书元数据提取的作者"
    )
    original_filename: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="上传时的原始文件名"
    )
    file_type: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="文件类型：pdf、epub 或 txt"
    )
    file_size: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="原始文件大小（字节）"
    )
    sha256: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="文件内容 SHA-256 摘要"
    )
    storage_path: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="相对于书库根目录的文件路径"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True, comment="处理状态"
    )
    processed_chunks: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="已经完成向量化的分块数"
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="书籍文本分块总数"
    )
    embedding_model: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="生成向量使用的模型"
    )
    embedding_dimension: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="向量维度"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="最近一次处理失败原因"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="最后更新时间（UTC）"
    )
