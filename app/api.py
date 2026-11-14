import asyncio
import contextlib
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.accounts import (
    SESSION_COOKIE_NAME,
    authenticate,
    consume_email_code,
    create_session,
    destroy_session,
    email_send_cooldown_error,
    generate_email_code,
    hash_password,
    normalize_email,
    normalize_username,
    store_email_code,
    validate_credentials,
    validate_email,
)
from app.config import get_settings
from app.db import get_db
from app.mailer import email_verification_enabled, send_verification_email
from app.models import (
    ChatMessage,
    DecisionSession,
    MealEntry,
    PantryItem,
    PreferenceNote,
    RecommendationFeedback,
    User,
    UserSession,
)
from app.prompts import PROMPT_VERSION
from app.schemas import (
    ChatReply,
    ChatMessageRead,
    ChatRequest,
    CityPick,
    DecisionOption,
    DecisionState,
    DeleteDataRequest,
    EmailCodeLoginRequest,
    EmailCodeRequest,
    GeoPoint,
    IpLocation,
    LoginRequest,
    MealCreate,
    MealRead,
    AdminUserCreate,
    AdminUserEmailRequest,
    EmailPasswordResetRequest,
    FeedbackCreate,
    FeedbackRead,
    PantryItemCreate,
    PantryItemRead,
    PasswordResetRequest,
    PreferenceCreate,
    PreferenceRead,
    PruneRequest,
    RegisterRequest,
    RecommendationRequest,
    RecommendationResponse,
    CookLesson,
    CookLessonRequest,
    UserRead,
)
from app.services import city_dishes, decision, nearby, nutrition, usage, watch
from app.services.meal_assistant import MAX_HISTORY_MESSAGES, DietAgent, summarize_older_history
from app.services import recipe_photos
from app.services.cook_teacher import CookTeacher
from app.rate_limit import limit_auth_calls, limit_model_calls, limit_recipe_photos
from app.security import (
    clear_session_cookie,
    client_host,
    get_current_user,
    require_admin,
    set_session_cookie,
)

logger = logging.getLogger("fandazi.api")

router = APIRouter(prefix="/api/v1")
public_router = APIRouter(prefix="/api/v1")
admin_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])
agent = DietAgent()
cook_teacher = CookTeacher()

MAX_DRAWS = decision.MAX_DRAWS


def _owned_session(db: Session, user: User, session_id: int) -> DecisionSession:
    """取属于当前用户的决策会话；不存在或不归属一律 404，避免探测他人会话。"""
    session = db.get(DecisionSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    return session


def _budget_exceeded() -> bool:
    budget = get_settings().daily_token_budget
    return bool(budget) and usage.today_tokens() >= budget


def _blocked_words(db: Session, user_id: int) -> list[str]:
    """必须从候选里排除的词：过敏 > 不吃 > 不爱吃。"""
    rows = list(
        db.scalars(
            select(PreferenceNote)
            .where(PreferenceNote.user_id == user_id)
            .where(PreferenceNote.active.is_(True))
            .where(PreferenceNote.kind.in_(("allergy", "avoid", "dislike")))
            .order_by(PreferenceNote.id)
        )
    )
    words: list[str] = []
    for row in rows:
        topic = (row.topic or "").strip()
        if topic and topic not in words:
            words.append(topic)
    return words


GOAL_TO_CONDITION = {"减脂": "fitness", "增肌": "fitness", "减肥": "fitness", "健身": "fitness",
                     "控糖": "diabetes", "低糖": "diabetes",
                     "低盐": "hypertension", "少盐": "hypertension"}


def _goal_condition(db: Session, user_id: int) -> str:
    """用户说过“在减脂/控糖”，即使没点模式按钮，也应该按对应模式选菜。"""
    rows = list(
        db.scalars(
            select(PreferenceNote)
            .where(PreferenceNote.user_id == user_id)
            .where(PreferenceNote.active.is_(True))
            .where(PreferenceNote.kind == "goal")
            .order_by(PreferenceNote.id.desc())
        )
    )
    for row in rows:
        text = (row.topic or "") + (row.text or "")
        for word, key in GOAL_TO_CONDITION.items():
            if word in text:
                return key
    return ""


def _recent_meals(db: Session, user_id: int, limit: int = 8) -> list[str]:
    """最近吃过的菜名，抽取时避开，避免连着几天推同一道。"""
    rows = list(
        db.scalars(
            select(MealEntry.description)
            .where(MealEntry.user_id == user_id)
            .order_by(MealEntry.eaten_at.desc())
            .limit(limit)
        )
    )
    return [row.strip() for row in rows if row and row.strip()]


def _liked_words(db: Session, user_id: int) -> list[str]:
    rows = list(
        db.scalars(
            select(PreferenceNote)
            .where(PreferenceNote.user_id == user_id)
            .where(PreferenceNote.active.is_(True))
            .where(PreferenceNote.kind == "like")
            .order_by(PreferenceNote.id)
        )
    )
    words: list[str] = []
    for row in rows:
        topic = (row.topic or "").strip()
        if topic and topic not in words:
            words.append(topic)
    return words


def _conflicting(topic: str, kind: str, db: Session, user_id: int) -> PreferenceNote | None:
    """新偏好和已有偏好直接冲突时，返回旧的那条。"""
    opposite = {"like": ("dislike", "avoid", "allergy"), "dislike": ("like",),
                "avoid": ("like",), "allergy": ("like",)}.get(kind, ())
    if not topic:
        return None
    return db.scalar(
        select(PreferenceNote)
        .where(PreferenceNote.user_id == user_id)
        .where(PreferenceNote.active.is_(True))
        .where(PreferenceNote.topic == topic)
        .where(PreferenceNote.kind.in_(opposite))
        .order_by(PreferenceNote.id.desc())
    )


def _persist_learned(db: Session, user_id: int, items: list[dict[str, str]]) -> tuple[list[PreferenceNote], list[str]]:
    created: list[PreferenceNote] = []
    replaced: list[str] = []
    for item in items:
        topic = (item.get("topic") or "").strip()
        kind = item["kind"]
        old = _conflicting(topic, kind, db, user_id)
        if old is not None:
            old.active = False
            replaced.append(old.text)
        exists = db.scalar(
            select(PreferenceNote)
            .where(PreferenceNote.user_id == user_id)
            .where(PreferenceNote.active.is_(True))
            .where(PreferenceNote.kind == kind)
            .where(PreferenceNote.topic == topic)
            .where(PreferenceNote.text == item["text"])
        )
        if exists is None:
            note = PreferenceNote(user_id=user_id, kind=kind, topic=topic, text=item["text"])
            db.add(note)
            created.append(note)
    return created, replaced


def _active_notes(db: Session, user_id: int) -> list[str]:
    rows = db.scalars(
        select(PreferenceNote)
        .where(PreferenceNote.user_id == user_id)
        .where(PreferenceNote.active.is_(True))
        .order_by(PreferenceNote.id.desc())
        .limit(20)
    )
    return [f"{row.kind}:{row.topic}:{row.text}" for row in rows]


@public_router.get("/health")
def health(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        logger.warning("health_database_unavailable", exc_info=True)
        raise HTTPException(status_code=503, detail="数据库不可用") from exc
    return {"status": "ok", "service": "fandazi-api", "database": "ok"}


@public_router.get("/recipe-photo-info", dependencies=[Depends(limit_recipe_photos)])
async def recipe_photo_info(name: str = Query(min_length=1, max_length=80)) -> dict:
    return await recipe_photos.photo_info(name)


@public_router.get("/recipe-photo", dependencies=[Depends(limit_recipe_photos)])
async def recipe_photo(
    name: str = Query(min_length=1, max_length=80),
    fallback: bool = Query(default=False),
) -> Response:
    content, content_type, found = await recipe_photos.fetch_photo(name, fallback_only=fallback)
    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=86400, stale-while-revalidate=604800",
            "X-Recipe-Photo": "pexels" if found else "placeholder",
        },
    )


@public_router.get("/watch-cover")
async def watch_cover(url: str = Query(min_length=1, max_length=2048)) -> Response:
    try:
        content, content_type = await watch.fetch_cover(url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="封面地址无效") from exc
    except watch.WatchSearchUnavailable as exc:
        raise HTTPException(status_code=502, detail="封面暂时无法加载") from exc
    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400, stale-while-revalidate=604800"},
    )


def _me_payload(user: User) -> UserRead:
    return UserRead.model_validate(user)


def _domain_error(allowed: list[str]) -> str:
    named = "QQ 邮箱" if allowed == ["qq.com"] else "、".join(allowed)
    return f"目前只支持 {named} 注册"


@public_router.get("/auth/config")
def auth_config() -> dict:
    """注册/邮箱登录能力：配了 SMTP 发件账号才开放邮箱注册。"""
    return {"email_enabled": email_verification_enabled()}


@public_router.post("/auth/email-code", dependencies=[Depends(limit_auth_calls)])
def request_email_code(payload: EmailCodeRequest, db: Session = Depends(get_db)) -> dict:
    """发送邮箱验证码：purpose=register 用于注册（邮箱须未占用），login 用于免密登录（邮箱须已绑定）。"""
    if not email_verification_enabled():
        raise HTTPException(status_code=403, detail="邮箱服务还没配置好，注册暂未开放")
    email = normalize_email(payload.email)
    error = validate_email(email)
    if error:
        raise HTTPException(status_code=422, detail=error)
    allowed = [
        domain.strip().lower()
        for domain in getattr(get_settings(), "email_allowed_domains", "").split(",")
        if domain.strip()
    ]
    if allowed and email.rsplit("@", 1)[-1] not in allowed:
        raise HTTPException(status_code=422, detail=_domain_error(allowed))
    registered = db.scalar(select(User).where(User.email == email)) is not None
    if payload.purpose == "register" and registered:
        raise HTTPException(status_code=409, detail="这个邮箱已经注册过了，直接登录吧")
    if payload.purpose in {"login", "reset"} and not registered:
        raise HTTPException(status_code=404, detail="这个邮箱没有绑定账号")
    cooldown = email_send_cooldown_error(db, email)
    if cooldown:
        raise HTTPException(status_code=429, detail=cooldown)
    code = generate_email_code()
    try:
        send_verification_email(email, code, purpose=payload.purpose)
    except Exception:
        logger.exception("email_code_send_failed to=%s", email)
        raise HTTPException(status_code=502, detail="验证码邮件发送失败，稍后再试") from None
    # 发信成功才落库，发送失败不占用发码冷却
    store_email_code(db, email, code, purpose=payload.purpose)
    return {"ok": True}


@public_router.post("/auth/login/email-code", response_model=UserRead,
                    dependencies=[Depends(limit_auth_calls)])
def login_with_email_code(
    payload: EmailCodeLoginRequest, response: Response, db: Session = Depends(get_db)
) -> User:
    """邮箱验证码免密登录：收到的验证码核对通过即登录该邮箱绑定的账号。"""
    email = normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="这个邮箱没有可登录的账号")
    code_error = consume_email_code(db, email, payload.email_code, purpose="login")
    if code_error:
        raise HTTPException(status_code=400, detail=code_error)
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    set_session_cookie(response, create_session(db, user))
    return user


@public_router.post("/auth/register", response_model=UserRead,
                    status_code=status.HTTP_201_CREATED,
                    dependencies=[Depends(limit_auth_calls)])
def register(payload: RegisterRequest, response: Response, db: Session = Depends(get_db)) -> User:
    """邮箱验证码注册：邮箱即账号（username 由邮箱派生），一个邮箱只能绑定一个账号。"""
    if not email_verification_enabled():
        raise HTTPException(status_code=403, detail="注册暂未开放，请稍后再试")
    email = normalize_email(payload.email)
    error = validate_email(email)
    if error:
        raise HTTPException(status_code=422, detail=error)
    allowed = [
        domain.strip().lower()
        for domain in getattr(get_settings(), "email_allowed_domains", "").split(",")
        if domain.strip()
    ]
    if allowed and email.rsplit("@", 1)[-1] not in allowed:
        raise HTTPException(status_code=422, detail=_domain_error(allowed))
    if len(payload.password) < 8:
        raise HTTPException(status_code=422, detail="密码至少 8 位")
    if db.scalar(select(User).where(User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="这个邮箱已经注册过了，直接登录吧")
    code_error = consume_email_code(db, email, payload.email_code, purpose="register")
    if code_error:
        raise HTTPException(status_code=400, detail=code_error)
    user = User(
        username=email,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        email=email,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    set_session_cookie(response, create_session(db, user))
    return user


@public_router.post("/auth/reset-password", dependencies=[Depends(limit_auth_calls)])
def reset_password(payload: EmailPasswordResetRequest, db: Session = Depends(get_db)) -> dict:
    """通过绑定邮箱验证码永久修改密码，并立即吊销该用户的全部会话。"""
    email = normalize_email(payload.email)
    user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise HTTPException(status_code=404, detail="这个邮箱没有可修改密码的账号")
    code_error = consume_email_code(db, email, payload.email_code, purpose="reset")
    if code_error:
        raise HTTPException(status_code=400, detail=code_error)
    user.password_hash = hash_password(payload.new_password)
    for stored in db.scalars(select(UserSession).where(UserSession.user_id == user.id)):
        db.delete(stored)
    db.commit()
    return {"ok": True}


@public_router.post("/auth/login", response_model=UserRead,
                    dependencies=[Depends(limit_auth_calls)])
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)) -> User:
    user = authenticate(db, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="邮箱或密码不对")
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    set_session_cookie(response, create_session(db, user))
    return user


@public_router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> dict:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        destroy_session(db, raw_token)
    clear_session_cookie(response)
    return {"ok": True}


@router.get("/auth/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.get("/meals", response_model=list[MealRead])
def list_meals(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[MealEntry]:
    statement = (
        select(MealEntry)
        .where(MealEntry.user_id == user.id)
        .order_by(MealEntry.eaten_at.desc())
    )
    if start is not None:
        statement = statement.where(MealEntry.eaten_at >= start)
    if end is not None:
        statement = statement.where(MealEntry.eaten_at < end)
    statement = statement.offset(offset).limit(limit)
    return list(db.scalars(statement))


@router.post("/meals", response_model=MealRead, status_code=status.HTTP_201_CREATED)
def create_meal(
    payload: MealCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> MealEntry:
    entry = MealEntry(
        user_id=user.id,
        description=payload.description,
        meal_type=payload.meal_type,
        eaten_at=payload.eaten_at or datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/meals/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meal(
    entry_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    entry = db.get(MealEntry, entry_id)
    if entry is None or entry.user_id != user.id:
        raise HTTPException(status_code=404, detail="找不到这条饮食记录")
    db.delete(entry)
    db.commit()


@router.get("/pantry", response_model=list[PantryItemRead])
def list_pantry(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[PantryItem]:
    return list(
        db.scalars(
            select(PantryItem)
            .where(PantryItem.user_id == user.id)
            .order_by(PantryItem.name)
        )
    )


@router.post("/pantry", response_model=PantryItemRead)
def save_pantry_item(
    payload: PantryItemCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> PantryItem:
    existing = db.scalar(
        select(PantryItem)
        .where(PantryItem.user_id == user.id)
        .where(func.lower(PantryItem.name) == payload.name.lower())
    )
    if existing is None:
        existing = PantryItem(user_id=user.id, name=payload.name, quantity=payload.quantity)
        db.add(existing)
    elif payload.quantity is not None:
        existing.quantity = payload.quantity
    db.commit()
    db.refresh(existing)
    return existing


@router.delete("/pantry/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pantry_item(
    item_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    item = db.get(PantryItem, item_id)
    if item is None or item.user_id != user.id:
        raise HTTPException(status_code=404, detail="找不到这项食材")
    db.delete(item)
    db.commit()


async def _build_recommendation(payload: RecommendationRequest, db: Session, user_id: int,
                                notify: Callable[[str], Awaitable[None]] | None = None) -> RecommendationResponse:
    started = time.perf_counter()
    recent = list(
        db.scalars(
            select(MealEntry.description)
            .where(MealEntry.user_id == user_id)
            .order_by(MealEntry.eaten_at.desc())
            .limit(10)
        )
    )
    pantry = list(
        db.scalars(
            select(PantryItem.name)
            .where(PantryItem.user_id == user_id)
            .order_by(PantryItem.name)
        )
    )
    notes = _active_notes(db, user_id)
    blocked = _blocked_words(db, user_id)
    db.rollback()
    try:
        explanation, recipes, source = await agent.recommend(
            payload, recent, pantry, notes, blocked, notify=notify
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not recipes:
        raise HTTPException(
            status_code=422,
            detail=explanation or "按你的忌口没找到合适的菜，换点食材再试试。",
        )
    recipe_photos.decorate_recipes(recipes)
    settings = get_settings()
    return RecommendationResponse(
        explanation=explanation,
        recipes=recipes,
        source=source,
        prompt_version=PROMPT_VERSION,
        model=getattr(settings, "openai_model", "") if source == "llm" else "",
        duration_ms=int((time.perf_counter() - started) * 1000),
        degraded=source != "llm",
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/recommendations", response_model=RecommendationResponse,
             dependencies=[Depends(limit_model_calls)])
async def recommend(
    payload: RecommendationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RecommendationResponse:
    if _budget_exceeded():
        raise HTTPException(status_code=429, detail="今天的模型额度用完了，明天再来。")
    return await _build_recommendation(payload, db, user.id)


@router.post("/recommendations/stream", dependencies=[Depends(limit_model_calls)])
async def recommend_stream(
    payload: RecommendationRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    if _budget_exceeded():
        raise HTTPException(status_code=429, detail="今天的模型额度用完了，明天再来。")

    async def events():
        yield _sse({"stage": "accepted"})
        yield _sse({"stage": "thinking", "message": "正在根据你的食材想菜单，通常需要二三十秒…"})
        # build() 在后台任务里跑，过程中的 notify 消息经队列转发成 progress 事件，
        # 避免整个 20~40 秒里前端只能干等。
        progress_queue: asyncio.Queue = asyncio.Queue()

        async def notify(message: str) -> None:
            await progress_queue.put(("progress", message))

        async def build():
            try:
                result = await _build_recommendation(payload, db, user.id, notify=notify)
            except HTTPException as exc:
                await progress_queue.put(("error", str(exc.detail)))
            except Exception:
                logger.exception("recommend_stream_failed")
                await progress_queue.put(("error", "服务暂时不可用，请稍后重试。"))
            else:
                await progress_queue.put(("done", result))

        build_task = asyncio.create_task(build())
        try:
            while True:
                kind, body = await progress_queue.get()
                if kind == "done":
                    yield _sse({"stage": "done", "payload": body.model_dump(mode="json")})
                    break
                if kind == "error":
                    yield _sse({"stage": "error", "message": str(body)})
                    break
                yield _sse({"stage": "progress", "message": str(body)})
        finally:
            if not build_task.done():
                build_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await build_task

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/preferences", response_model=list[PreferenceRead])
def list_preferences(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[PreferenceNote]:
    return list(
        db.scalars(
            select(PreferenceNote)
            .where(PreferenceNote.user_id == user.id)
            .where(PreferenceNote.active.is_(True))
            .order_by(PreferenceNote.id.desc())
        )
    )


@router.post("/preferences", response_model=PreferenceRead, status_code=status.HTTP_201_CREATED)
def create_preference(
    payload: PreferenceCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> PreferenceNote:
    """手动加一条偏好时，也一样处理冲突：爱吃牛肉 会顶掉 不吃牛肉，反之亦然。"""
    created, _ = _persist_learned(
        db, user.id, [{"kind": payload.kind, "topic": payload.topic.strip(), "text": payload.text}]
    )
    db.commit()
    for note in created:
        db.refresh(note)
    if created:
        return created[0]
    existing = db.scalar(
        select(PreferenceNote)
        .where(PreferenceNote.user_id == user.id)
        .where(PreferenceNote.active.is_(True))
        .where(PreferenceNote.kind == payload.kind)
        .where(PreferenceNote.topic == payload.topic.strip())
        .order_by(PreferenceNote.id.desc())
    )
    if existing is None:
        raise HTTPException(status_code=409, detail="这条记录已存在")
    return existing


@router.delete("/preferences/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_preference(
    note_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> None:
    note = db.get(PreferenceNote, note_id)
    if note is None or note.user_id != user.id:
        raise HTTPException(status_code=404, detail="找不到这条偏好")
    note.active = False
    db.commit()


def _is_silent_memory_message(text: str) -> bool:
    """纯偏好陈述：只在说喜欢/不喜欢，没有提问，不占用聊天区。"""
    return bool(DietAgent._rule_extract(text)) and not DietAgent._looks_like_decision_question(text)


@router.get("/chat/history", response_model=list[ChatMessageRead])
def chat_history(
    user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[ChatMessage]:
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.id.desc())
        .limit(40)
    )
    return [
        row for row in reversed(list(rows))
        if not (row.role == "user" and _is_silent_memory_message(row.content))
    ]


def _chat_context(db: Session, user_id: int,
                  limit: int = MAX_HISTORY_MESSAGES) -> tuple[list[dict[str, str]], str]:
    rows = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id)
            .order_by(ChatMessage.id)
        )
    )
    normalized = [
        {"role": "assistant" if row.role == "agent" else "user", "content": row.content}
        for row in rows
    ]
    older = normalized[:-limit] if len(normalized) > limit else []
    return normalized[-limit:], summarize_older_history(older) if older else ""


@router.post("/chat", response_model=ChatReply,
             dependencies=[Depends(limit_model_calls)])
async def chat(
    payload: ChatRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> ChatReply:
    if _budget_exceeded():
        raise HTTPException(status_code=429, detail="今天的模型额度用完了，明天再来。")
    notes = _active_notes(db, user.id)
    history, earlier_summary = _chat_context(db, user.id)
    reply, learned, source = await agent.chat(payload.message, notes, history, earlier_summary)
    created, replaced = _persist_learned(db, user.id, learned)
    # 偏好更新只通过「我的口味」和小提示反馈，不写进对话气泡。
    if reply:
        db.add(ChatMessage(user_id=user.id, role="user", content=payload.message))
        db.add(ChatMessage(user_id=user.id, role="agent", content=reply))
    db.commit()
    for note in created:
        db.refresh(note)
    return ChatReply(
        reply=reply,
        learned=[PreferenceRead.model_validate(n) for n in created],
        replaced=replaced[:3],
        source=source,
        prompt_version=PROMPT_VERSION,
    )


@router.post("/cook-lessons", response_model=CookLesson,
             dependencies=[Depends(limit_model_calls)])
async def cook_lesson(
    payload: CookLessonRequest, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> CookLesson:
    if _budget_exceeded():
        raise HTTPException(status_code=429, detail="今天的模型额度用完了，明天再来。")
    # 只传用户记下的 topic，展开和食材匹配放在服务层，不依赖模型自觉。
    topics = _blocked_words(db, user.id)
    return await cook_teacher.teach(payload.dish, topics)


def _nearby_places(session: DecisionSession) -> list | None:
    """定位后返回当地菜（静态特产 + AI 整理的日常菜）；没定位则返回 None。"""
    if session.mode != "takeout":
        return None
    answers = session.answers or {}
    merged: list = []
    for key in ("_local_places", "_ai_places"):
        raw = answers.get(key)
        if isinstance(raw, list):
            merged.extend(item for item in raw if isinstance(item, dict))
    label = str(answers.get("_location_label") or "")
    if label:
        merged.extend(city_dishes.cached(label))
    return merged or None


def _pick_choice(session: DecisionSession, blocked: list[str], drawn: list[str],
                 recent: list[str], condition: str) -> str:
    places = _nearby_places(session)
    if places is None:
        return decision.draw(
            session.mode, session.answers or {}, drawn, condition, blocked, recent
        )
    answers = session.answers or {}
    picked = nearby.choose(places, answers, blocked, drawn, source="all")
    if picked:
        return picked["choice"]
    return decision.draw(session.mode, answers, drawn, condition, blocked, recent)


def _no_match_suggestion(location_label: str, answers: dict, has_location_data: bool) -> str:
    if not has_location_data:
        return "按你的条件没找到合适的，换个口味或模式试试。"
    labels = {
        "taste": {"sweet": "甜口", "salty": "咸鲜", "spicy": "辣口", "light": "清淡"},
        "staple": {"rice": "米饭", "noodle": "面食", "nocarb": "不吃主食"},
        "heaviness": {"heavy": "丰盛", "simple": "简单"},
    }
    criteria = [labels[key].get(answers.get(key), "") for key in labels]
    criteria = [item for item in criteria if item]
    selected = f"“{'、'.join(criteria)}”这组条件" if criteria else "当前条件"
    area = f"{location_label}的" if location_label else ""
    return f"{area}本地菜库和常见菜库里都没有符合{selected}的菜，可以重新选择口味、主食或分量。"


def _state(session: DecisionSession, db: Session, user_id: int, auto_draw: bool = False) -> DecisionState:
    """返回当前状态。auto_draw=True 时，问答一结束就自动抽一次。"""
    total = len(decision.mode_for(session.mode).steps)
    blocked = _blocked_words(db, user_id)
    filter_words = decision.expand_blocked(blocked)
    recent = _recent_meals(db, user_id)
    drawn = list(session.drawn or [])
    location_label = str((session.answers or {}).get("_location_label") or "")
    location_data_version = int((session.answers or {}).get("_location_data_version") or 0)
    places = _nearby_places(session)
    step = decision.next_step(session.mode, session.answers or {})

    if step is None and not drawn and auto_draw:
        choice = _pick_choice(session, filter_words, [], recent, session.condition)
        if choice:
            drawn = [choice]
            session.drawn = drawn
            db.commit()
            db.refresh(session)

    if drawn:
        if places is None:
            alts = decision.alternatives(
                session.mode, session.answers or {}, drawn, session.condition,
                filter_words, recent, 3, drawn[-1],
            )
            reasons = decision.explain(
                session.mode, session.answers or {}, drawn[-1], session.condition, blocked, recent
            )
        else:
            local_alts = nearby.alternatives(
                places, session.answers or {}, filter_words, drawn, 3, source="all"
            )
            common_alts = decision.alternatives(
                session.mode, session.answers or {}, drawn, session.condition,
                filter_words, recent, 3, drawn[-1],
            )
            alts = list(dict.fromkeys(local_alts + common_alts))[:3]
            picked = nearby.choose(places, session.answers or {}, filter_words, drawn[:-1], source="all")
            if picked and picked["choice"] == drawn[-1]:
                reasons = picked["reasons"]
            else:
                reasons = decision.explain(
                    session.mode, session.answers or {}, drawn[-1], session.condition, blocked, recent
                )
        return DecisionState(
            session_id=session.id,
            mode=session.mode,
            step=session.step,
            total_steps=total,
            location_label=location_label,
            location_data_version=location_data_version,
            condition=session.condition,
            question="",
            options=[],
            can_draw=len(drawn) < MAX_DRAWS,
            draws=len(drawn),
            result=decision.result_line(
                session.mode, session.answers or {}, drawn[-1], session.condition, blocked
            ),
            alternatives=alts,
            reasons=reasons,
            finished=True,
            note=_note_line(db, user_id, blocked),
            suggestion=decision.exhausted_hint(len(drawn), drawn[-1]),
        )

    if step is None:
        return DecisionState(
            session_id=session.id,
            mode=session.mode,
            step=session.step,
            total_steps=total,
            location_label=location_label,
            location_data_version=location_data_version,
            condition=session.condition,
            can_draw=False,
            result="",
            finished=True,
            note=_note_line(db, user_id, blocked),
            suggestion=_no_match_suggestion(location_label, session.answers or {}, places is not None),
        )

    return DecisionState(
        session_id=session.id,
        mode=session.mode,
        step=session.step,
        total_steps=total,
        location_label=location_label,
        location_data_version=location_data_version,
        condition=session.condition,
        question=step.question,
        options=[DecisionOption(id=key, label=label, hint=hint) for key, label, hint in step.options],
        can_draw=True,
        note=_note_line(db, user_id, blocked),
        suggestion="",
    )


def _note_line(db: Session, user_id: int, blocked: list[str]) -> str:
    if blocked:
        return f"会避开 {'、'.join(blocked[:3])}"
    liked = _liked_words(db, user_id)
    return f"记住你爱吃 {'、'.join(liked[:3])}" if liked else ""


@router.get("/decisions/{session_id}", response_model=DecisionState)
def read_decision(
    session_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> DecisionState:
    session = _owned_session(db, user, session_id)
    return _state(session, db, user.id)


@router.post("/decisions", response_model=DecisionState)
def start_decision(
    mode: str = "takeout",
    condition: str = "",
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DecisionState:
    if mode not in ("takeout", "cook"):
        raise HTTPException(status_code=422, detail="不支持的模式")
    if not condition:
        condition = _goal_condition(db, user.id)
    session = DecisionSession(user_id=user.id, mode=mode, condition=condition, answers={}, drawn=[], step=0)
    db.add(session)
    db.commit()
    db.refresh(session)
    return _state(session, db, user.id)


def _apply_location(session: DecisionSession, db: Session, user_id: int, location_label: str) -> DecisionState:
    """把定位结果写入会话并立即抽一次，供坐标与城市名两条路径复用。"""
    answers = dict(session.answers or {})
    answers["_location_label"] = location_label
    answers["_location_data_version"] = nearby.REGIONAL_CATALOG_VERSION
    answers["_local_places"] = nearby.regional_places(location_label)
    answers["_ai_places"] = city_dishes.cached(location_label)
    # AI 日常菜在后台线程预热：模型慢时不拖住授权流程，下一轮抽签即可用
    city_dishes.prefetch(location_label)
    session.answers = answers
    session.drawn = []
    db.commit()
    db.refresh(session)
    return _state(session, db, user_id, auto_draw=True)


@router.post("/decisions/{session_id}/location", response_model=DecisionState)
def save_location(
    session_id: int,
    payload: GeoPoint,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DecisionState:
    session = _owned_session(db, user, session_id)
    if session.mode != "takeout":
        raise HTTPException(status_code=422, detail="只有点外卖模式才能按所在地区推荐")
    key = get_settings().amap_key
    location_label = nearby.fetch_location_label(payload.lng, payload.lat, key)
    answers = dict(session.answers or {})
    answers["_lat"] = payload.lat
    answers["_lng"] = payload.lng
    session.answers = answers
    return _apply_location(session, db, user.id, location_label)


@router.post("/decisions/{session_id}/city", response_model=DecisionState)
def save_city(
    session_id: int,
    payload: CityPick,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DecisionState:
    """用户手动选城市：HTTP 下浏览器不给坐标时的兜底路径。"""
    session = _owned_session(db, user, session_id)
    if session.mode != "takeout":
        raise HTTPException(status_code=422, detail="只有点外卖模式才能按所在地区推荐")
    label = nearby.city_label(payload.city)
    if not label:
        raise HTTPException(status_code=422, detail="城市名不能为空")
    return _apply_location(session, db, user.id, label)


@router.get("/location/ip", response_model=IpLocation)
def ip_location(request: Request, ip: str = "") -> IpLocation:
    """按客户端 IP 粗判城市。Docker 映射后取不到真实 IP 时返回空，前端据此提示手动选择。

    ip 参数仅用于排查：传了就查这个地址，不传则用请求来源地址。
    """
    key = get_settings().amap_key
    target = ip.strip() or client_host(request)
    city = nearby.locate_by_ip(target, key)
    return IpLocation(city=nearby.city_label(city), source="ip" if city else "")


@router.post("/decisions/{session_id}/answer", response_model=DecisionState)
def answer_decision(
    session_id: int,
    option: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DecisionState:
    session = _owned_session(db, user, session_id)
    step = decision.next_step(session.mode, session.answers or {})
    if step is None:
        raise HTTPException(status_code=409, detail="这一轮已经问完了")
    valid = [key for key, _, _ in step.options]
    if option not in valid:
        raise HTTPException(status_code=422, detail="选项无效")
    answers = dict(session.answers or {})
    answers[step.key] = option
    session.answers = answers
    session.step = len(answers)
    db.commit()
    db.refresh(session)
    return _state(session, db, user.id, auto_draw=True)


@router.post("/decisions/{session_id}/draw", response_model=DecisionState)
@router.post("/decisions/{session_id}/again", response_model=DecisionState)
def draw_decision(
    session_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> DecisionState:
    return _redraw(session_id, user, db)


@router.post("/decisions/{session_id}/feedback", response_model=FeedbackRead)
def save_recommendation_feedback(
    session_id: int,
    payload: FeedbackCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RecommendationFeedback:
    session = _owned_session(db, user, session_id)
    if payload.dish_name not in (session.drawn or []):
        raise HTTPException(status_code=422, detail="这道菜不属于本次推荐结果")
    feedback = db.scalar(
        select(RecommendationFeedback)
        .where(RecommendationFeedback.session_id == session_id)
        .where(RecommendationFeedback.dish_name == payload.dish_name)
    )
    if feedback is None:
        feedback = RecommendationFeedback(
            session_id=session_id,
            dish_name=payload.dish_name,
            action=payload.action,
            rating=payload.rating,
        )
        db.add(feedback)
    else:
        feedback.action = payload.action
        feedback.rating = payload.rating
    if payload.preference:
        label = "爱吃" if payload.preference == "like" else "不爱吃"
        _persist_learned(
            db,
            user.id,
            [{
                "kind": payload.preference,
                "topic": payload.dish_name,
                "text": f"{label}{payload.dish_name}",
            }],
        )
    db.commit()
    db.refresh(feedback)
    return feedback


@router.post("/decisions/{session_id}/pick", response_model=DecisionState)
def pick_alternative(
    session_id: int,
    choice: str,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> DecisionState:
    """用户点了备选，直接把它变成这一轮的结果。"""
    session = _owned_session(db, user, session_id)
    blocked = decision.expand_blocked(_blocked_words(db, user.id))
    if decision.is_blocked(choice, blocked):
        raise HTTPException(status_code=422, detail="这个和你的忌口冲突")
    drawn = list(session.drawn or [])
    if len(drawn) >= MAX_DRAWS:
        return _state(session, db, user.id)
    drawn.append(choice)
    session.drawn = drawn
    session.finished = True
    db.commit()
    db.refresh(session)
    return _state(session, db, user.id)


def _redraw(session_id: int, user: User, db: Session) -> DecisionState:
    session = _owned_session(db, user, session_id)
    drawn = list(session.drawn or [])
    if len(drawn) >= MAX_DRAWS:
        return _state(session, db, user.id)
    choice = _pick_choice(
        session, decision.expand_blocked(_blocked_words(db, user.id)), drawn,
        _recent_meals(db, user.id), session.condition
    )
    if not choice:
        return _state(session, db, user.id)
    drawn.append(choice)
    session.drawn = drawn
    session.finished = True
    db.commit()
    db.refresh(session)
    return _state(session, db, user.id)


@admin_router.get("/usage")
def get_usage() -> dict:
    return usage.snapshot()


@router.get("/watch-search")
async def watch_search(
    q: str = Query(default="", max_length=80),
    category: str = Query(
        default="all",
        pattern="^(all|动画|动漫|电视剧|电影|电影解说|纪录片|综艺|美剧|短剧|生活)$",
    ),
    page: int = Query(default=1, ge=1, le=150),
    visit_id: str = Query(default="", max_length=80),
) -> dict:
    try:
        return await watch.search_videos(q, category, page, visit_id)
    except watch.WatchSearchUnavailable as exc:
        raise HTTPException(status_code=502, detail="哔哩哔哩搜索暂时不可用") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/nutrition-summary")
def nutrition_summary(
    period: str = Query(default="day", pattern="^(day|week)$"),
    date_anchor: date = Query(alias="date"),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    end = date_anchor + timedelta(days=1 if period == "day" else 7)
    start_at = datetime.combine(date_anchor, datetime.min.time())
    end_at = datetime.combine(end, datetime.min.time())
    entries = list(
        db.scalars(
            select(MealEntry)
            .where(MealEntry.user_id == user.id)
            .where(MealEntry.eaten_at >= start_at)
            .where(MealEntry.eaten_at < end_at)
            .order_by(MealEntry.eaten_at)
        )
    )
    return nutrition.summarize(entries, period, date_anchor)


@router.get("/export")
def export_data(
    response: Response,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """导出当前用户自己的全部数据。"""
    own_sessions = select(DecisionSession.id).where(DecisionSession.user_id == user.id)
    meals = list(
        db.scalars(
            select(MealEntry)
            .where(MealEntry.user_id == user.id)
            .order_by(MealEntry.eaten_at.desc())
        )
    )
    pantry = list(
        db.scalars(
            select(PantryItem)
            .where(PantryItem.user_id == user.id)
            .order_by(PantryItem.name)
        )
    )
    preferences = list(
        db.scalars(
            select(PreferenceNote)
            .where(PreferenceNote.user_id == user.id)
            .order_by(PreferenceNote.id)
        )
    )
    chats = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.user_id == user.id)
            .order_by(ChatMessage.id)
        )
    )
    sessions = list(
        db.scalars(
            select(DecisionSession)
            .where(DecisionSession.user_id == user.id)
            .order_by(DecisionSession.id)
        )
    )
    feedback = list(
        db.scalars(
            select(RecommendationFeedback)
            .where(RecommendationFeedback.session_id.in_(own_sessions))
            .order_by(RecommendationFeedback.id)
        )
    )
    # 导出内容含全部个人数据，禁止中间层缓存。
    response.headers["Cache-Control"] = "no-store"
    logger.info(
        "data_export client=%s user=%s meals=%d pantry=%d preferences=%d chats=%d sessions=%d feedback=%d",
        client_host(request), user.username, len(meals), len(pantry), len(preferences),
        len(chats), len(sessions), len(feedback),
    )
    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "meals": [MealRead.model_validate(m).model_dump(mode="json") for m in meals],
        "pantry": [PantryItemRead.model_validate(p).model_dump(mode="json") for p in pantry],
        "preferences": [
            {"id": p.id, "kind": p.kind, "topic": p.topic, "text": p.text,
             "active": p.active, "created_at": p.created_at.isoformat() if p.created_at else None}
            for p in preferences
        ],
        "chat_messages": [
            {"id": c.id, "role": c.role, "content": c.content,
             "created_at": c.created_at.isoformat() if c.created_at else None}
            for c in chats
        ],
        "decision_sessions": [
            {"id": s.id, "mode": s.mode, "condition": s.condition, "answers": s.answers,
             "drawn": s.drawn, "finished": s.finished,
             "created_at": s.created_at.isoformat() if s.created_at else None}
            for s in sessions
        ],
        "recommendation_feedback": [
            {"id": f.id, "session_id": f.session_id, "dish_name": f.dish_name,
             "action": f.action, "rating": f.rating,
             "created_at": f.created_at.isoformat() if f.created_at else None}
            for f in feedback
        ],
    }


@admin_router.post("/maintenance/prune")
def prune_old_data(
    payload: PruneRequest, request: Request, db: Session = Depends(get_db)
) -> dict[str, int]:
    """全站清理过期决策与聊天，仅管理员可执行。"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=payload.days)
    old_sessions = select(DecisionSession.id).where(
        DecisionSession.finished.is_(True), DecisionSession.created_at < cutoff
    )
    feedback_result = db.execute(
        delete(RecommendationFeedback).where(RecommendationFeedback.session_id.in_(old_sessions))
    )
    session_result = db.execute(
        delete(DecisionSession).where(
            DecisionSession.finished.is_(True), DecisionSession.created_at < cutoff
        )
    )
    chat_result = db.execute(delete(ChatMessage).where(ChatMessage.created_at < cutoff))
    db.commit()
    deleted_counts = {
        "chat_messages": chat_result.rowcount or 0,
        "decision_sessions": session_result.rowcount or 0,
        "recommendation_feedback": feedback_result.rowcount or 0,
    }
    logger.info(
        "maintenance_prune client=%s days=%d deleted=%s",
        client_host(request), payload.days, deleted_counts,
    )
    return deleted_counts


@router.delete("/data")
def delete_all_data(
    payload: DeleteDataRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """清空当前用户自己的全部数据（不影响其他账号）。"""
    if payload.confirm != "DELETE":
        raise HTTPException(status_code=400, detail='需要 confirm="DELETE" 才会清空数据')
    own_sessions = select(DecisionSession.id).where(DecisionSession.user_id == user.id)
    deleted: dict[str, int] = {}
    feedback_result = db.execute(
        delete(RecommendationFeedback).where(RecommendationFeedback.session_id.in_(own_sessions))
    )
    deleted["recommendation_feedback"] = feedback_result.rowcount or 0
    for model in (DecisionSession, ChatMessage, PreferenceNote, PantryItem, MealEntry):
        result = db.execute(delete(model).where(model.user_id == user.id))
        deleted[model.__tablename__] = result.rowcount or 0
    db.commit()
    logger.warning(
        "user_data_deleted client=%s user=%s counts=%s", client_host(request), user.username, deleted
    )
    return {"deleted": deleted}


def _per_user_counts(db: Session) -> dict[int, dict[str, int]]:
    counts: dict[int, dict[str, int]] = {}
    for model, key in (
        (MealEntry, "meals"),
        (PantryItem, "pantry"),
        (PreferenceNote, "preferences"),
        (DecisionSession, "decisions"),
        (ChatMessage, "chats"),
    ):
        rows = db.execute(select(model.user_id, func.count()).group_by(model.user_id)).all()
        for uid, count in rows:
            counts.setdefault(uid, {})[key] = count
    return counts


@admin_router.get("/admin/users")
def admin_list_users(db: Session = Depends(get_db)) -> list[dict]:
    counts = _per_user_counts(db)
    users = list(db.scalars(select(User).order_by(User.id)))
    return [
        {
            "id": u.id,
            "username": u.username,
            "display_name": u.display_name,
            "email": u.email,
            "is_admin": u.is_admin,
            "is_active": u.is_active,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            **counts.get(u.id, {"meals": 0, "pantry": 0, "preferences": 0, "decisions": 0, "chats": 0}),
        }
        for u in users
    ]


@admin_router.post("/admin/users", status_code=status.HTTP_201_CREATED)
def admin_create_user(payload: AdminUserCreate, db: Session = Depends(get_db)) -> dict:
    """管理员手动开号：不检查邀请码。"""
    error = validate_credentials(payload.username, payload.password)
    if error:
        raise HTTPException(status_code=422, detail=error)
    username = normalize_username(payload.username)
    if db.scalar(select(User).where(User.username == username)) is not None:
        raise HTTPException(status_code=409, detail="这个用户名已经被占用了")
    user = User(
        username=username,
        password_hash=hash_password(payload.password),
        display_name=payload.display_name,
        is_admin=payload.is_admin,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username}


@admin_router.patch("/admin/users/{user_id}/email")
def admin_bind_user_email(
    user_id: int, payload: AdminUserEmailRequest, db: Session = Depends(get_db)
) -> dict:
    """管理员为没有邮箱的旧账号补绑定邮箱，不接触用户密码。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    email = normalize_email(payload.email)
    error = validate_email(email)
    if error:
        raise HTTPException(status_code=422, detail=error)
    if user.email and user.email != email:
        raise HTTPException(status_code=409, detail="这个账号已经绑定邮箱，不能覆盖")
    existing = db.scalar(select(User).where(User.email == email).where(User.id != user_id))
    if existing is not None:
        raise HTTPException(status_code=409, detail="这个邮箱已经绑定其他账号")
    user.email = email
    db.commit()
    return {"ok": True, "email": email}


@admin_router.post("/admin/users/{user_id}/reset-password")
def admin_reset_password(user_id: int, payload: PasswordResetRequest, db: Session = Depends(get_db)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.password_hash = hash_password(payload.new_password)
    # 重置密码后踢掉该用户的所有已登录会话
    for stored in db.scalars(select(UserSession).where(UserSession.user_id == user_id)):
        db.delete(stored)
    db.commit()
    return {"ok": True}


@admin_router.post("/admin/users/{user_id}/toggle-active")
def admin_toggle_active(user_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)) -> dict:
    if user_id == admin.id:
        raise HTTPException(status_code=422, detail="不能停用自己的账号")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    user.is_active = not user.is_active
    if not user.is_active:
        for stored in db.scalars(select(UserSession).where(UserSession.user_id == user_id)):
            db.delete(stored)
    db.commit()
    return {"ok": True, "is_active": user.is_active}


@admin_router.get("/admin/overview")
def admin_overview(db: Session = Depends(get_db)) -> dict:
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    def _count(model, *filters) -> int:
        statement = select(func.count()).select_from(model)
        for condition in filters:
            statement = statement.where(condition)
        return db.scalar(statement) or 0

    return {
        "users": {
            "total": _count(User),
            "active": _count(User, User.is_active.is_(True)),
            "new_this_week": _count(User, User.created_at >= week_ago),
        },
        "meals_last_24h": _count(MealEntry, MealEntry.eaten_at >= day_ago),
        "totals": {
            "meals": _count(MealEntry),
            "pantry": _count(PantryItem),
            "preferences": _count(PreferenceNote),
            "decisions": _count(DecisionSession),
            "chats": _count(ChatMessage),
            "feedback": _count(RecommendationFeedback),
        },
        "ai_usage": usage.snapshot(),
    }


@admin_router.get("/admin/users/{user_id}/summary")
def admin_user_summary(user_id: int, db: Session = Depends(get_db)) -> dict:
    """管理员查看某个用户的具体数据（只读）。"""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    meals = list(
        db.scalars(
            select(MealEntry)
            .where(MealEntry.user_id == user_id)
            .order_by(MealEntry.eaten_at.desc())
            .limit(20)
        )
    )
    preferences = list(
        db.scalars(
            select(PreferenceNote)
            .where(PreferenceNote.user_id == user_id)
            .where(PreferenceNote.active.is_(True))
            .order_by(PreferenceNote.id.desc())
        )
    )
    chats = list(
        db.scalars(
            select(ChatMessage)
            .where(ChatMessage.user_id == user_id)
            .order_by(ChatMessage.id.desc())
            .limit(20)
        )
    )
    decisions = db.scalar(
        select(func.count()).select_from(DecisionSession).where(DecisionSession.user_id == user_id)
    ) or 0
    return {
        "user": {
            "id": user.id, "username": user.username, "display_name": user.display_name,
            "email": user.email,
            "is_admin": user.is_admin, "is_active": user.is_active,
            "created_at": user.created_at.isoformat() if user.created_at else None,
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        },
        "decision_count": decisions,
        "meals": [
            {"id": m.id, "description": m.description, "meal_type": m.meal_type,
             "eaten_at": m.eaten_at.isoformat() if m.eaten_at else None}
            for m in meals
        ],
        "preferences": [
            {"id": p.id, "kind": p.kind, "topic": p.topic, "text": p.text}
            for p in preferences
        ],
        "chat_messages": [
            {"id": c.id, "role": c.role, "content": c.content,
             "created_at": c.created_at.isoformat() if c.created_at else None}
            for c in chats
        ],
    }


# ========== 隐私保护和数据安全接口 ==========

@router.get("/account/sessions")
def list_my_sessions(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    """查看当前账号的所有活跃登录会话（设备管理）"""
    current_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    current_hash = hash_token(current_token) if current_token else ""
    
    sessions = list(
        db.scalars(
            select(UserSession)
            .where(UserSession.user_id == user.id)
            .where(UserSession.expires_at > datetime.now(timezone.utc))
            .order_by(UserSession.created_at.desc())
        )
    )
    
    return [
        {
            "id": s.id,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "expires_at": s.expires_at.isoformat() if s.expires_at else None,
            "is_current": s.token_hash == current_hash,
        }
        for s in sessions
    ]


@router.delete("/account/sessions/{session_id}")
def revoke_session(
    session_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """注销指定的登录会话（踢出某个设备）"""
    session = db.get(UserSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    current_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    is_current = session.token_hash == hash_token(current_token) if current_token else False
    
    db.delete(session)
    db.commit()
    
    logger.info(
        "session_revoked client=%s user=%s session_id=%s is_current=%s",
        client_host(request), user.username, session_id, is_current
    )
    
    return {"ok": True, "revoked_current_session": is_current}


@router.post("/account/sessions/revoke-all")
def revoke_all_sessions(
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """注销所有其他设备的登录会话（保留当前会话）"""
    current_token = request.cookies.get(SESSION_COOKIE_NAME, "")
    current_hash = hash_token(current_token) if current_token else ""
    
    revoked = 0
    for session in db.scalars(
        select(UserSession).where(UserSession.user_id == user.id)
    ):
        if session.token_hash != current_hash:
            db.delete(session)
            revoked += 1
    
    db.commit()
    logger.info(
        "all_sessions_revoked client=%s user=%s count=%s",
        client_host(request), user.username, revoked
    )
    
    return {"ok": True, "revoked_count": revoked}


@router.get("/account/export")
def export_my_data(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """导出当前用户的所有数据（GDPR 合规）"""
    
    # 用户基本信息
    user_info = {
        "id": user.id,
        "username": user.username,
        "email": user.email,
        "display_name": user.display_name,
        "is_admin": user.is_admin,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }
    
    # 饮食记录
    meals = [
        {
            "id": m.id,
            "description": m.description,
            "meal_type": m.meal_type,
            "eaten_at": m.eaten_at.isoformat() if m.eaten_at else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in db.scalars(
            select(MealEntry)
            .where(MealEntry.user_id == user.id)
            .order_by(MealEntry.eaten_at.desc())
        )
    ]
    
    # 食材库存
    pantry = [
        {
            "id": p.id,
            "name": p.name,
            "quantity": p.quantity,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in db.scalars(
            select(PantryItem)
            .where(PantryItem.user_id == user.id)
            .order_by(PantryItem.name)
        )
    ]
    
    # 偏好设置
    preferences = [
        {
            "id": pref.id,
            "kind": pref.kind,
            "topic": pref.topic,
            "text": pref.text,
            "active": pref.active,
            "created_at": pref.created_at.isoformat() if pref.created_at else None,
        }
        for pref in db.scalars(
            select(PreferenceNote)
            .where(PreferenceNote.user_id == user.id)
            .order_by(PreferenceNote.id)
        )
    ]
    
    # 聊天记录
    chat_messages = [
        {
            "id": c.id,
            "role": c.role,
            "content": c.content,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in db.scalars(
            select(ChatMessage)
            .where(ChatMessage.user_id == user.id)
            .order_by(ChatMessage.created_at)
        )
    ]
    
    # 决策会话
    decision_sessions = [
        {
            "id": d.id,
            "mode": d.mode,
            "condition": d.condition,
            "answers": d.answers,
            "step": d.step,
            "finished": d.finished,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in db.scalars(
            select(DecisionSession)
            .where(DecisionSession.user_id == user.id)
            .order_by(DecisionSession.created_at.desc())
        )
    ]
    
    logger.info(
        "data_exported user=%s meals=%d pantry=%d prefs=%d chats=%d sessions=%d",
        user.username, len(meals), len(pantry), len(preferences), 
        len(chat_messages), len(decision_sessions)
    )
    
    return {
        "user_info": user_info,
        "meals": meals,
        "pantry": pantry,
        "preferences": preferences,
        "chat_messages": chat_messages,
        "decision_sessions": decision_sessions,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "format_version": "1.0",
    }


@router.post("/account/deactivate")
def deactivate_my_account(
    payload: dict,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """永久注销账号（删除账号及所有关联数据，不可恢复）"""
    
    # 验证密码
    password = payload.get("password", "")
    if not verify_password(password, user.password_hash):
        raise HTTPException(status_code=400, detail="密码不正确")
    
    # 验证确认字段
    if payload.get("confirm") != "DEACTIVATE":
        raise HTTPException(status_code=400, detail='需要 confirm="DEACTIVATE" 才能注销账号')
    
    # 管理员不能自己注销（防止误操作导致无管理员）
    if user.is_admin:
        admin_count = db.scalar(
            select(func.count()).select_from(User).where(User.is_admin.is_(True))
        ) or 0
        if admin_count <= 1:
            raise HTTPException(
                status_code=403,
                detail="你是唯一的管理员，无法注销账号。请先指定其他管理员。"
            )
    
    username = user.username
    user_id = user.id
    
    # 删除所有关联数据
    own_sessions_ids = select(DecisionSession.id).where(DecisionSession.user_id == user_id)
    
    deleted: dict[str, int] = {}
    
    # 删除推荐反馈
    result = db.execute(
        delete(RecommendationFeedback)
        .where(RecommendationFeedback.session_id.in_(own_sessions_ids))
    )
    deleted["recommendation_feedback"] = result.rowcount or 0
    
    # 删除用户数据
    for model in (DecisionSession, ChatMessage, PreferenceNote, PantryItem, MealEntry):
        result = db.execute(delete(model).where(model.user_id == user_id))
        deleted[model.__tablename__] = result.rowcount or 0
    
    # 删除所有登录会话
    result = db.execute(delete(UserSession).where(UserSession.user_id == user_id))
    deleted["user_sessions"] = result.rowcount or 0
    
    # 删除账号本身
    db.delete(user)
    db.commit()
    
    # 清除当前 Cookie
    clear_session_cookie(response)
    
    logger.warning(
        "account_deactivated client=%s user=%s user_id=%s deleted=%s",
        client_host(request), username, user_id, deleted,
    )
    return {"ok": True, "deleted": deleted}

