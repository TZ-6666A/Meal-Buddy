import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import (
    ChatMessage,
    DecisionSession,
    MealEntry,
    PantryItem,
    PreferenceNote,
    RecommendationFeedback,
)
from app.prompts import PROMPT_VERSION
from app.schemas import (
    ChatReply,
    ChatMessageRead,
    ChatRequest,
    DecisionOption,
    DecisionState,
    DeleteDataRequest,
    MealCreate,
    MealRead,
    FeedbackCreate,
    FeedbackRead,
    PantryItemCreate,
    PantryItemRead,
    PreferenceCreate,
    PreferenceRead,
    PruneRequest,
    RecommendationRequest,
    RecommendationResponse,
)
from app.services import decision, usage
from app.services.meal_assistant import MAX_HISTORY_MESSAGES, DietAgent, summarize_older_history
from app.rate_limit import limit_model_calls

logger = logging.getLogger("fandazi.api")

router = APIRouter(prefix="/api/v1")
public_router = APIRouter(prefix="/api/v1")
agent = DietAgent()

MAX_DRAWS = decision.MAX_DRAWS


def _budget_exceeded() -> bool:
    budget = get_settings().daily_token_budget
    return bool(budget) and usage.today_tokens() >= budget


def _blocked_words(db: Session) -> list[str]:
    """必须从候选里排除的词：过敏 > 不吃 > 不爱吃。"""
    rows = list(
        db.scalars(
            select(PreferenceNote)
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


def _blocked_expanded(db: Session) -> list[str]:
    """过滤用：把“牛肉”这类粗粒度忌口展开成具体食材。"""
    return decision.expand_blocked(_blocked_words(db))


GOAL_TO_CONDITION = {"减脂": "fitness", "增肌": "fitness", "减肥": "fitness", "健身": "fitness",
                     "控糖": "diabetes", "低糖": "diabetes",
                     "低盐": "hypertension", "少盐": "hypertension"}


def _goal_condition(db: Session) -> str:
    """用户说过“在减脂/控糖”，即使没点模式按钮，也应该按对应模式选菜。"""
    rows = list(
        db.scalars(
            select(PreferenceNote)
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


def _recent_meals(db: Session, limit: int = 8) -> list[str]:
    """最近吃过的菜名，抽取时避开，避免连着几天推同一道。"""
    rows = list(
        db.scalars(
            select(MealEntry.description).order_by(MealEntry.eaten_at.desc()).limit(limit)
        )
    )
    return [row.strip() for row in rows if row and row.strip()]


def _liked_words(db: Session) -> list[str]:
    rows = list(
        db.scalars(
            select(PreferenceNote)
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


def _conflicting(topic: str, kind: str, db: Session) -> PreferenceNote | None:
    """新偏好和已有偏好直接冲突时，返回旧的那条。"""
    opposite = {"like": ("dislike", "avoid", "allergy"), "dislike": ("like",),
                "avoid": ("like",), "allergy": ("like",)}.get(kind, ())
    if not topic:
        return None
    return db.scalar(
        select(PreferenceNote)
        .where(PreferenceNote.active.is_(True))
        .where(PreferenceNote.topic == topic)
        .where(PreferenceNote.kind.in_(opposite))
        .order_by(PreferenceNote.id.desc())
    )


def _persist_learned(db: Session, items: list[dict[str, str]]) -> tuple[list[PreferenceNote], list[str]]:
    created: list[PreferenceNote] = []
    replaced: list[str] = []
    for item in items:
        topic = (item.get("topic") or "").strip()
        kind = item["kind"]
        old = _conflicting(topic, kind, db)
        if old is not None:
            old.active = False
            replaced.append(old.text)
        exists = db.scalar(
            select(PreferenceNote)
            .where(PreferenceNote.active.is_(True))
            .where(PreferenceNote.kind == kind)
            .where(PreferenceNote.topic == topic)
            .where(PreferenceNote.text == item["text"])
        )
        if exists is None:
            note = PreferenceNote(kind=kind, topic=topic, text=item["text"])
            db.add(note)
            created.append(note)
    return created, replaced


def _active_notes(db: Session) -> list[str]:
    rows = db.scalars(
        select(PreferenceNote).where(PreferenceNote.active.is_(True)).order_by(PreferenceNote.id.desc()).limit(20)
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


@router.get("/meals", response_model=list[MealRead])
def list_meals(
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[MealEntry]:
    statement = select(MealEntry).order_by(MealEntry.eaten_at.desc())
    if start is not None:
        statement = statement.where(MealEntry.eaten_at >= start)
    if end is not None:
        statement = statement.where(MealEntry.eaten_at < end)
    statement = statement.offset(offset).limit(limit)
    return list(db.scalars(statement))


@router.post("/meals", response_model=MealRead, status_code=status.HTTP_201_CREATED)
def create_meal(payload: MealCreate, db: Session = Depends(get_db)) -> MealEntry:
    entry = MealEntry(
        description=payload.description,
        meal_type=payload.meal_type,
        eaten_at=payload.eaten_at or datetime.now(timezone.utc),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


@router.delete("/meals/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_meal(entry_id: int, db: Session = Depends(get_db)) -> None:
    entry = db.get(MealEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="找不到这条饮食记录")
    db.delete(entry)
    db.commit()


@router.get("/pantry", response_model=list[PantryItemRead])
def list_pantry(db: Session = Depends(get_db)) -> list[PantryItem]:
    return list(db.scalars(select(PantryItem).order_by(PantryItem.name)))


@router.post("/pantry", response_model=PantryItemRead)
def save_pantry_item(payload: PantryItemCreate, db: Session = Depends(get_db)) -> PantryItem:
    existing = db.scalar(select(PantryItem).where(func.lower(PantryItem.name) == payload.name.lower()))
    if existing is None:
        existing = PantryItem(name=payload.name, quantity=payload.quantity)
        db.add(existing)
    elif payload.quantity is not None:
        existing.quantity = payload.quantity
    db.commit()
    db.refresh(existing)
    return existing


@router.delete("/pantry/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_pantry_item(item_id: int, db: Session = Depends(get_db)) -> None:
    item = db.get(PantryItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="找不到这项食材")
    db.delete(item)
    db.commit()


async def _build_recommendation(payload: RecommendationRequest, db: Session) -> RecommendationResponse:
    recent = list(db.scalars(select(MealEntry.description).order_by(MealEntry.eaten_at.desc()).limit(10)))
    pantry = list(db.scalars(select(PantryItem.name).order_by(PantryItem.name)))
    notes = _active_notes(db)
    blocked = _blocked_words(db)
    db.rollback()
    try:
        explanation, recipes, source = await agent.recommend(
            payload, recent, pantry, notes, blocked
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not recipes:
        raise HTTPException(
            status_code=422,
            detail="按你的忌口没找到合适的菜，换点食材再试试。",
        )
    return RecommendationResponse(
        explanation=explanation, recipes=recipes, source=source, prompt_version=PROMPT_VERSION
    )


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.post("/recommendations", response_model=RecommendationResponse,
             dependencies=[Depends(limit_model_calls)])
async def recommend(payload: RecommendationRequest, db: Session = Depends(get_db)) -> RecommendationResponse:
    if _budget_exceeded():
        raise HTTPException(status_code=429, detail="今天的模型额度用完了，明天再来。")
    return await _build_recommendation(payload, db)


@router.post("/recommendations/stream", dependencies=[Depends(limit_model_calls)])
async def recommend_stream(payload: RecommendationRequest,
                           db: Session = Depends(get_db)) -> StreamingResponse:
    if _budget_exceeded():
        raise HTTPException(status_code=429, detail="今天的模型额度用完了，明天再来。")

    async def events():
        yield _sse({"stage": "accepted"})
        yield _sse({"stage": "thinking"})
        try:
            result = await _build_recommendation(payload, db)
            yield _sse({"stage": "done", "payload": result.model_dump(mode="json")})
        except HTTPException as exc:
            yield _sse({"stage": "error", "message": str(exc.detail)})
        except Exception:
            logger.exception("recommend_stream_failed")
            yield _sse({"stage": "error", "message": "服务暂时不可用，请稍后重试。"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/preferences", response_model=list[PreferenceRead])
def list_preferences(db: Session = Depends(get_db)) -> list[PreferenceNote]:
    return list(db.scalars(select(PreferenceNote).where(PreferenceNote.active.is_(True)).order_by(PreferenceNote.id.desc())))


@router.post("/preferences", response_model=PreferenceRead, status_code=status.HTTP_201_CREATED)
def create_preference(payload: PreferenceCreate, db: Session = Depends(get_db)) -> PreferenceNote:
    """手动加一条偏好时，也一样处理冲突：爱吃牛肉 会顶掉 不吃牛肉，反之亦然。"""
    created, _ = _persist_learned(
        db, [{"kind": payload.kind, "topic": payload.topic.strip(), "text": payload.text}]
    )
    db.commit()
    for note in created:
        db.refresh(note)
    if created:
        return created[0]
    existing = db.scalar(
        select(PreferenceNote)
        .where(PreferenceNote.active.is_(True))
        .where(PreferenceNote.kind == payload.kind)
        .where(PreferenceNote.topic == payload.topic.strip())
        .order_by(PreferenceNote.id.desc())
    )
    if existing is None:
        raise HTTPException(status_code=409, detail="这条记录已存在")
    return existing


@router.delete("/preferences/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_preference(note_id: int, db: Session = Depends(get_db)) -> None:
    note = db.get(PreferenceNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="找不到这条偏好")
    note.active = False
    db.commit()


@router.get("/chat/history", response_model=list[ChatMessageRead])
def chat_history(db: Session = Depends(get_db)) -> list[ChatMessage]:
    rows = db.scalars(select(ChatMessage).order_by(ChatMessage.id.desc()).limit(40))
    return list(reversed(list(rows)))


def _chat_context(db: Session, limit: int = MAX_HISTORY_MESSAGES) -> tuple[list[dict[str, str]], str]:
    rows = list(db.scalars(select(ChatMessage).order_by(ChatMessage.id)))
    normalized = [
        {"role": "assistant" if row.role == "agent" else "user", "content": row.content}
        for row in rows
    ]
    older = normalized[:-limit] if len(normalized) > limit else []
    return normalized[-limit:], summarize_older_history(older) if older else ""


@router.post("/chat", response_model=ChatReply,
             dependencies=[Depends(limit_model_calls)])
async def chat(payload: ChatRequest, db: Session = Depends(get_db)) -> ChatReply:
    if _budget_exceeded():
        raise HTTPException(status_code=429, detail="今天的模型额度用完了，明天再来。")
    notes = _active_notes(db)
    history, earlier_summary = _chat_context(db)
    reply, learned, source = await agent.chat(payload.message, notes, history, earlier_summary)
    created, replaced = _persist_learned(db, learned)
    if replaced:
        reply += f"（已更新之前的记录：{'、'.join(replaced[:3])}）"
    db.add(ChatMessage(role="user", content=payload.message))
    db.add(ChatMessage(role="agent", content=reply))
    db.commit()
    for note in created:
        db.refresh(note)
    return ChatReply(
        reply=reply,
        learned=[PreferenceRead.model_validate(n) for n in created],
        source=source,
        prompt_version=PROMPT_VERSION,
    )


def _state(session: DecisionSession, db: Session, auto_draw: bool = False) -> DecisionState:
    """返回当前状态。auto_draw=True 时，问答一结束就自动抽一次。"""
    total = len(decision.mode_for(session.mode).steps)
    blocked = _blocked_words(db)
    filter_words = decision.expand_blocked(blocked)
    recent = _recent_meals(db)
    drawn = list(session.drawn or [])
    step = decision.next_step(session.mode, session.answers or {})

    if step is None and not drawn and auto_draw:
        choice = decision.draw(
            session.mode, session.answers or {}, [], session.condition, filter_words, recent
        )
        if choice:
            drawn = [choice]
            session.drawn = drawn
            db.commit()
            db.refresh(session)

    if drawn:
        return DecisionState(
            session_id=session.id,
            mode=session.mode,
            step=session.step,
            total_steps=total,
            condition=session.condition,
            question="",
            options=[],
            can_draw=len(drawn) < MAX_DRAWS,
            draws=len(drawn),
            result=decision.result_line(
                session.mode, session.answers or {}, drawn[-1], session.condition, blocked
            ),
            alternatives=decision.alternatives(
                session.mode, session.answers or {}, drawn, session.condition,
                filter_words, recent, 3, drawn[-1],
            ),
            reasons=decision.explain(
                session.mode, session.answers or {}, drawn[-1], session.condition, blocked, recent
            ),
            finished=True,
            note=_note_line(db, blocked),
            suggestion=decision.exhausted_hint(len(drawn), drawn[-1]),
        )

    if step is None:
        return DecisionState(
            session_id=session.id,
            mode=session.mode,
            step=session.step,
            total_steps=total,
            condition=session.condition,
            can_draw=False,
            result="",
            finished=True,
            note=_note_line(db, blocked),
            suggestion="按你的条件没找到合适的，换个口味或模式试试。",
        )

    return DecisionState(
        session_id=session.id,
        mode=session.mode,
        step=session.step,
        total_steps=total,
        condition=session.condition,
        question=step.question,
        options=[DecisionOption(id=key, label=label, hint=hint) for key, label, hint in step.options],
        can_draw=True,
        note=_note_line(db, blocked),
        suggestion="",
    )


def _note_line(db: Session, blocked: list[str]) -> str:
    if blocked:
        return f"会避开 {'、'.join(blocked[:3])}"
    liked = _liked_words(db)
    return f"记住你爱吃 {'、'.join(liked[:3])}" if liked else ""


@router.post("/decisions", response_model=DecisionState)
def start_decision(mode: str = "takeout", condition: str = "", db: Session = Depends(get_db)) -> DecisionState:
    if mode not in ("takeout", "cook"):
        raise HTTPException(status_code=422, detail="不支持的模式")
    if not condition:
        condition = _goal_condition(db)
    session = DecisionSession(mode=mode, condition=condition, answers={}, drawn=[], step=0)
    db.add(session)
    db.commit()
    db.refresh(session)
    return _state(session, db)


@router.post("/decisions/{session_id}/answer", response_model=DecisionState)
def answer_decision(session_id: int, option: str, db: Session = Depends(get_db)) -> DecisionState:
    session = db.get(DecisionSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
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
    return _state(session, db, auto_draw=True)


@router.post("/decisions/{session_id}/draw", response_model=DecisionState)
@router.post("/decisions/{session_id}/again", response_model=DecisionState)
def draw_decision(session_id: int, db: Session = Depends(get_db)) -> DecisionState:
    return _redraw(session_id, db)


@router.post("/decisions/{session_id}/feedback", response_model=FeedbackRead)
def save_recommendation_feedback(
    session_id: int, payload: FeedbackCreate, db: Session = Depends(get_db)
) -> RecommendationFeedback:
    session = db.get(DecisionSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
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
    db.commit()
    db.refresh(feedback)
    return feedback


@router.post("/decisions/{session_id}/pick", response_model=DecisionState)
def pick_alternative(session_id: int, choice: str, db: Session = Depends(get_db)) -> DecisionState:
    """用户点了备选，直接把它变成这一轮的结果。"""
    session = db.get(DecisionSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    blocked = decision.expand_blocked(_blocked_words(db))
    if decision.is_blocked(choice, blocked):
        raise HTTPException(status_code=422, detail="这个和你的忌口冲突")
    drawn = list(session.drawn or [])
    if len(drawn) >= MAX_DRAWS:
        return _state(session, db)
    drawn.append(choice)
    session.drawn = drawn
    session.finished = True
    db.commit()
    db.refresh(session)
    return _state(session, db)


def _redraw(session_id: int, db: Session) -> DecisionState:
    session = db.get(DecisionSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    drawn = list(session.drawn or [])
    if len(drawn) >= MAX_DRAWS:
        return _state(session, db)
    choice = decision.draw(
        session.mode,
        session.answers or {},
        drawn,
        session.condition,
        decision.expand_blocked(_blocked_words(db)),
        _recent_meals(db),
    )
    if not choice:
        return _state(session, db)
    drawn.append(choice)
    session.drawn = drawn
    session.finished = True
    db.commit()
    db.refresh(session)
    return _state(session, db)


@router.get("/usage")
def get_usage() -> dict:
    return usage.snapshot()


@router.get("/export")
def export_data(db: Session = Depends(get_db)) -> dict:
    meals = list(db.scalars(select(MealEntry).order_by(MealEntry.eaten_at.desc())))
    pantry = list(db.scalars(select(PantryItem).order_by(PantryItem.name)))
    preferences = list(db.scalars(select(PreferenceNote).order_by(PreferenceNote.id)))
    chats = list(db.scalars(select(ChatMessage).order_by(ChatMessage.id)))
    sessions = list(db.scalars(select(DecisionSession).order_by(DecisionSession.id)))
    feedback = list(db.scalars(select(RecommendationFeedback).order_by(RecommendationFeedback.id)))
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


@router.post("/maintenance/prune")
def prune_old_data(payload: PruneRequest, db: Session = Depends(get_db)) -> dict[str, int]:
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
    return {
        "chat_messages": chat_result.rowcount or 0,
        "decision_sessions": session_result.rowcount or 0,
        "recommendation_feedback": feedback_result.rowcount or 0,
    }


@router.delete("/data")
def delete_all_data(payload: DeleteDataRequest, db: Session = Depends(get_db)) -> dict:
    if payload.confirm != "DELETE":
        raise HTTPException(status_code=400, detail='需要 confirm="DELETE" 才会清空数据')
    deleted: dict[str, int] = {}
    for model in (ChatMessage, RecommendationFeedback, DecisionSession,
                  PreferenceNote, PantryItem, MealEntry):
        result = db.execute(delete(model))
        deleted[model.__tablename__] = result.rowcount or 0
    db.commit()
    logger.warning("all_data_deleted counts=%s", deleted)
    return {"deleted": deleted}
