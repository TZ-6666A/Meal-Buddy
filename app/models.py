from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    """登录账号。username/email 一律小写存储以实现不区分大小写的唯一性。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(200))
    # 邮箱注册验证后绑定；可空（老账号/管理员手动建号可没有），小写存储
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# 邮箱唯一（可空：老账号可以没有邮箱；Postgres/SQLite 的唯一索引都允许多个 NULL）
Index("uq_users_email", User.email, unique=True)


class UserSession(Base):
    """服务端登录会话：Cookie 只放随机 token，库里存其 SHA-256，便于吊销。"""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EmailVerification(Base):
    """邮箱验证码：库里只存 SHA-256，验证成功或过期后作废。"""

    __tablename__ = "email_verifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    code_hash: Mapped[str] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32), default="register")
    attempts: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MealEntry(Base):
    __tablename__ = "meal_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    description: Mapped[str] = mapped_column(Text)
    meal_type: Mapped[str] = mapped_column(String(40), default="unspecified")
    eaten_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class PantryItem(Base):
    __tablename__ = "pantry_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # 全局唯一改为按用户唯一（uq_pantry_user_name，见下方函数式索引），不同用户可以存同名食材
    name: Mapped[str] = mapped_column(String(120))
    quantity: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


Index("uq_pantry_user_name", PantryItem.user_id, func.lower(PantryItem.name), unique=True)


class PreferenceNote(Base):
    """用户长期习惯：喜欢/不喜欢/忌口/过敏/目标。"""

    __tablename__ = "preference_notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(24), default="like")
    topic: Mapped[str] = mapped_column(String(60), default="")
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DecisionSession(Base):
    """一个问答链会话，记录已选答案与已抽到的结果，用于避免重复。"""

    __tablename__ = "decision_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    mode: Mapped[str] = mapped_column(String(24), default="takeout")
    condition: Mapped[str] = mapped_column(String(24), default="")
    answers: Mapped[dict] = mapped_column(JSON, default=dict)
    drawn: Mapped[list] = mapped_column(JSON, default=list)
    step: Mapped[int] = mapped_column(default=0)
    finished: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RecommendationFeedback(Base):
    """归属通过 session_id 间接决定（会话属于某个用户）。"""

    __tablename__ = "recommendation_feedback"
    __table_args__ = (
        UniqueConstraint("session_id", "dish_name", name="uq_feedback_session_dish"),
        CheckConstraint("action IN ('accept', 'reject', 'eaten')", name="ck_feedback_action"),
        CheckConstraint("rating IS NULL OR (rating >= 1 AND rating <= 5)", name="ck_feedback_rating"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(ForeignKey("decision_sessions.id", ondelete="CASCADE"), nullable=False)
    dish_name: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    rating: Mapped[int | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
