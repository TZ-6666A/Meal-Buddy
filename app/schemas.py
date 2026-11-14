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
    image_url: str = ""
    image_fallback_url: str = ""
    image_alt: str = ""
    image_photographer: str = ""
    image_credit: str = ""
    image_credit_url: str = ""


class RecommendationRequest(BaseModel):
    ingredients: list[str] = Field(default_factory=list, max_length=30)
    available_minutes: int = Field(default=30, ge=5, le=240)
    dietary_preferences: str = Field(default="", max_length=1000)
    condition: str = Field(default="", max_length=24)


class RecommendationResponse(BaseModel):
    explanation: str
    recipes: list[Recipe]
    # source: llm=真实模型；demo=未配置模型；fallback=模型失败后的降级结果
    source: str
    prompt_version: str = ""
    model: str = ""
    duration_ms: int = 0
    degraded: bool = False


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


class CookLessonRequest(BaseModel):
    dish: str = Field(min_length=1, max_length=40)

    @field_validator("dish")
    @classmethod
    def strip_dish(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("菜名不能为空")
        if len(cleaned) > 40:
            raise ValueError("菜名不能超过 40 字")
        return cleaned


class CookIngredient(BaseModel):
    name: str
    amount: str
    role: str


class CookStep(BaseModel):
    order: int
    text: str
    heat: str = ""
    minutes: str = ""
    caution: str = ""


class CookLesson(BaseModel):
    dish: str
    servings: str = "2人份"
    note: str = ""
    ingredients: list[CookIngredient]
    steps: list[CookStep]
    warnings: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)


class ChatReply(BaseModel):
    reply: str
    learned: list[PreferenceRead] = Field(default_factory=list)
    replaced: list[str] = Field(default_factory=list)
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


class GeoPoint(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class CityPick(BaseModel):
    city: str = Field(min_length=1, max_length=40)


class IpLocation(BaseModel):
    city: str = ""
    source: str = ""


class DecisionState(BaseModel):
    session_id: int
    mode: str
    step: int
    total_steps: int = 0
    location_label: str = ""
    location_data_version: int = 0
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
    preference: Literal["like", "dislike"] | None = None
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


class RegisterRequest(BaseModel):
    """邮箱即账号：username 由邮箱派生（小写存储，唯一由 email 唯一索引保证）。"""

    email: str = Field(min_length=3, max_length=254)
    email_code: str = Field(min_length=1, max_length=12)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=40)

    @field_validator("email")
    @classmethod
    def normalize_email_field(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("email_code", "password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


class LoginRequest(BaseModel):
    # username 字段兼容老调用方：登录时填邮箱（或老账号的用户名）都行
    username: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username", "password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class UserRead(BaseModel):
    username: str
    display_name: str | None
    email: str | None
    is_admin: bool

    model_config = {"from_attributes": True}


class EmailCodeRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    # register=注册；login=免密登录；reset=修改密码
    purpose: str = Field(default="register", pattern="^(register|login|reset)$")

    @field_validator("email")
    @classmethod
    def strip_email(cls, value: str) -> str:
        return value.strip().lower()


class EmailCodeLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    email_code: str = Field(min_length=1, max_length=12)

    @field_validator("email")
    @classmethod
    def strip_email(cls, value: str) -> str:
        return value.strip().lower()


class PasswordResetRequest(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class EmailPasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    email_code: str = Field(min_length=1, max_length=12)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("email")
    @classmethod
    def strip_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("email_code", "new_password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()


class AdminUserEmailRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def strip_email(cls, value: str) -> str:
        return value.strip().lower()


class AdminUserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=40)
    is_admin: bool = False

    @field_validator("username", "password")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        return cleaned or None


# ========== 隐私保护和数据安全相关 Schema ==========

class DeactivateAccountRequest(BaseModel):
    """注销账号请求（需要密码确认）"""
    password: str = Field(min_length=1, max_length=128)
    confirm: str = Field(pattern="^DEACTIVATE$")
    
    @field_validator("password")
    @classmethod
    def strip_password(cls, value: str) -> str:
        return value.strip()


class ExportDataResponse(BaseModel):
    """数据导出响应"""
    user_info: dict
    meals: list[dict]
    pantry: list[dict]
    preferences: list[dict]
    chat_messages: list[dict]
    decision_sessions: list[dict]
    exported_at: str
    format_version: str = "1.0"


class UserSessionInfo(BaseModel):
    """用户会话信息（用于设备管理）"""
    id: int
    created_at: str
    expires_at: str
    is_current: bool
    ip_address: str | None = None
    user_agent: str | None = None
    
    model_config = {"from_attributes": True}


class RevokeSessionRequest(BaseModel):
    """注销指定会话"""
    session_id: int
