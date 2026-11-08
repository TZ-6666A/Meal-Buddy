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
    EmailVerification,
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
# 宽松邮箱格式：有本地部分、一个 @、域名带点即可，细节交给收件验证
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]+\.[^@\s]{2,}$")

_OWNED_MODELS = (MealEntry, PantryItem, PreferenceNote, DecisionSession, ChatMessage)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> str | None:
    """返回错误文案；None 表示校验通过。"""
    if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
        return "邮箱格式看起来不对，检查一下再试"
    return None


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
    """用户名或邮箱 + 密码登录。"""
    key = normalize_username(username)
    user = db.scalar(select(User).where(User.username == key))
    if user is None and "@" in key:
        user = db.scalar(select(User).where(User.email == key))
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


# ---------- 邮箱验证码 ----------

def _aware(value: datetime) -> datetime:
    """SQLite 会返回 naive datetime，统一按 UTC 处理。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _email_code_ttl_minutes() -> int:
    return getattr(get_settings(), "email_code_ttl_minutes", 10) or 10


def email_send_cooldown_error(db: Session, email: str) -> str | None:
    """发码限频：60 秒内不发第二条；每天每邮箱最多 10 条。"""
    settings = get_settings()
    rows = list(
        db.scalars(
            select(EmailVerification)
            .where(EmailVerification.email == email)
            .order_by(EmailVerification.id.desc())
            .limit(int(getattr(settings, "email_code_max_per_day", 10) or 10))
        )
    )
    now = datetime.now(timezone.utc)
    resend_seconds = int(getattr(settings, "email_code_resend_seconds", 60) or 60)
    max_per_day = int(getattr(settings, "email_code_max_per_day", 10) or 10)
    if rows:
        latest = _aware(rows[0].created_at)
        if now - latest < timedelta(seconds=resend_seconds):
            return "验证码刚发过，等一会再收新的"
    sent_today = sum(1 for row in rows if now - _aware(row.created_at) < timedelta(hours=24))
    if sent_today >= max_per_day:
        return "这个邮箱今天的验证码次数用完了，明天再来"
    return None


def generate_email_code() -> str:
    """生成 6 位数字验证码（不落库，发信成功后才存）。"""
    return f"{secrets.randbelow(1_000_000):06d}"


def store_email_code(db: Session, email: str, code: str) -> None:
    """验证码落库（只存哈希）。发信成功后调用，避免发信失败占用发码冷却。"""
    db.add(
        EmailVerification(
            email=email,
            code_hash=hash_token(code),
            purpose="register",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=_email_code_ttl_minutes()),
        )
    )
    db.commit()


def consume_email_code(db: Session, email: str, code: str) -> str | None:
    """核销验证码。返回错误文案；None 表示验证通过（验证码随即作废）。"""
    stored = db.scalar(
        select(EmailVerification)
        .where(EmailVerification.email == email)
        .where(EmailVerification.used_at.is_(None))
        .order_by(EmailVerification.id.desc())
    )
    if stored is None:
        return "请先获取邮箱验证码"
    if _aware(stored.expires_at) < datetime.now(timezone.utc):
        return "验证码已过期，重新获取一个"
    if stored.attempts >= 5:
        return "错误次数太多，这条验证码作废了，重新获取一个"
    if not hmac.compare_digest(hash_token(code.strip()), stored.code_hash):
        stored.attempts += 1
        db.commit()
        return "验证码不对，再看看邮件"
    stored.used_at = datetime.now(timezone.utc)
    db.commit()
    return None


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

    # 配了 ADMIN_EMAIL 就绑到管理员账号，之后可以用邮箱登录
    admin_email = normalize_email(getattr(settings, "admin_email", "") or "")
    if admin_email and admin.email != admin_email:
        existing = db.scalar(select(User).where(User.email == admin_email))
        if existing is None:
            admin.email = admin_email
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
