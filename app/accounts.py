"""账号与登录会话的领域逻辑。

刻意只用标准库：scrypt 做密码哈希、secrets 生成会话 token，
避免为服务器新增依赖（生产环境 pip 下载很慢）。
"""

import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    ChatMessage,
    DecisionSession,
    MealEntry,
    PantryItem,
    PreferenceNote,
    User,
    UserSession,
)

SESSION_COOKIE_NAME = "fandazi_session"

# scrypt 参数：内存约 16MB，兼顾安全与单进程并发
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1

# \w 在 Unicode 模式下已覆盖中文、字母、数字、下划线
_USERNAME_RE = re.compile(r"^[\w]{2,24}$", re.UNICODE)

_OWNED_MODELS = (MealEntry, PantryItem, PreferenceNote, DecisionSession, ChatMessage)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def validate_credentials(username: str, password: str) -> str | None:
    """返回错误文案；None 表示校验通过。"""
    if not _USERNAME_RE.fullmatch(username):
        return "用户名需 2-24 位，只能是中文、字母、数字或下划线"
    if len(password) < 8:
        return "密码至少 8 位"
    return None


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=32,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest.hex(), hash_hex)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def create_session(db: Session, user: User) -> str:
    """新建登录会话，返回要写入 Cookie 的原始 token。"""
    raw_token = secrets.token_urlsafe(32)
    days = getattr(get_settings(), "session_ttl_days", 30) or 30
    session = UserSession(
        token_hash=hash_token(raw_token),
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(days=days),
    )
    db.add(session)
    db.commit()
    return raw_token


def destroy_session(db: Session, raw_token: str) -> None:
    stored = db.scalar(select(UserSession).where(UserSession.token_hash == hash_token(raw_token)))
    if stored is not None:
        db.delete(stored)
        db.commit()


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.username == normalize_username(username)))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def ensure_bootstrap(db: Session) -> int:
    """启动引导（幂等）：确保管理员存在，历史无主数据归入管理员名下。

    返回本次归属的历史记录条数。生产环境 admin_password 由
    validate_for_startup 强制配置；开发环境缺省用 admin/dev12345。
    """
    settings = get_settings()
    admin = db.scalar(select(User).where(User.is_admin.is_(True)).order_by(User.id).limit(1))
    if admin is None:
        username = normalize_username(getattr(settings, "admin_username", "") or "admin")
        password = getattr(settings, "admin_password", "") or "dev12345"
        admin = db.scalar(select(User).where(User.username == username))
        if admin is None:
            admin = User(username=username, password_hash=hash_password(password), is_admin=True)
            db.add(admin)
        else:
            admin.is_admin = True
        db.commit()

    moved = 0
    for model in _OWNED_MODELS:
        result = db.execute(
            update(model).where(model.user_id.is_(None)).values(user_id=admin.id)
        )
        moved += result.rowcount or 0
    if moved:
        db.commit()
    return moved
