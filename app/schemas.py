from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_INJECTION_PHRASES = (
    "忽略以上", "忽略之前", "忽略上面", "ignore previous", "ignore all previous",
    "ignore above", "system prompt", "系统提示词", "你的指令",
)


class MealCreate(BaseModel):
    description: str = Field(min_length=1, max_length=2000)
    meal_type: str = Field(default="unspecified", max_length=40)
    eaten_at: datetime | None = None

    @field_validator("description", "meal_type")
    @classmethod
    def strip_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能是空白内容")
        return cleaned


class MealRead(BaseModel):
    id: int
    description: str
    meal_type: str
    eaten_at: datetime

    model_config = {"from_attributes": True}


class PantryItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    quantity: str | None = Field(default=None, max_length=80)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能是空白内容")
        return cleaned

    @field_validator("quantity")
    @classmethod
    def strip_quantity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class PantryItemRead(BaseModel):
    id: int
    name: str
    quantity: str | None

    model_config = {"from_attributes": True}


class RecipeIngredient(BaseModel):
    name: str
    amount: str


class Recipe(BaseModel):
    name: str
    description: str
    prep_minutes: int = Field(ge=1, le=240)
    ingredients: list[RecipeIngredient]
    steps: list[str]
    tags: list[str] = Field(default_factory=list)


class RecommendationRequest(BaseModel):
    ingredients: list[str] = Field(default_factory=list, max_length=30)
    available_minutes: int = Field(default=30, ge=5, le=240)
    dietary_preferences: str = Field(default="", max_length=1000)
    condition: str = Field(default="", max_length=24)


class RecommendationResponse(BaseModel):
    explanation: str
    recipes: list[Recipe]
    source: str
    prompt_version: str = ""


class PreferenceCreate(BaseModel):
    kind: str = Field(default="like", max_length=24)
    topic: str = Field(default="", max_length=60)
    text: str = Field(min_length=1, max_length=500)

    @field_validator("kind")
    @classmethod
    def normalize_kind(cls, value: str) -> str:
        allowed = {"like", "dislike", "avoid", "allergy", "goal"}
        cleaned = value.strip().lower()
        return cleaned if cleaned in allowed else "like"

    @field_validator("topic", "text")
    @classmethod
    def reject_prompt_injection(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("不能是空白内容")
        if any(ch in cleaned for ch in ("\n", "\r", "<", ">")):
            raise ValueError("不能包含换行或尖括号")
        lowered = cleaned.lower()
        if any(phrase in lowered for phrase in _INJECTION_PHRASES):
            raise ValueError("这条内容看起来像指令，不像口味偏好")
        return cleaned


class PreferenceRead(BaseModel):
    id: int
    kind: str
    topic: str
    text: str

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ChatReply(BaseModel):
    reply: str
    learned: list[PreferenceRead] = Field(default_factory=list)
    source: str = "rule"
    prompt_version: str = ""


class ChatMessageRead(BaseModel):
    id: int
    role: Literal["user", "agent"]
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DeleteDataRequest(BaseModel):
    confirm: str = ""


class DecisionOption(BaseModel):
    id: str
    label: str
    hint: str = ""


class DecisionState(BaseModel):
    session_id: int
    mode: str
    step: int
    total_steps: int = 0
    condition: str = ""
    question: str = ""
    options: list[DecisionOption] = Field(default_factory=list)
    can_draw: bool = False
    draws: int = 0
    result: str = ""
    alternatives: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    finished: bool = False
    note: str = ""
    suggestion: str = ""


class FeedbackCreate(BaseModel):
    dish_name: str = Field(min_length=1, max_length=120)
    action: Literal["accept", "reject", "eaten"]
    rating: int | None = Field(default=None, ge=1, le=5)

    @field_validator("dish_name")
    @classmethod
    def strip_dish_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("菜名不能为空")
        return cleaned


class FeedbackRead(BaseModel):
    id: int
    session_id: int
    dish_name: str
    action: str
    rating: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class PruneRequest(BaseModel):
    days: int = Field(default=90, ge=1, le=3650)
