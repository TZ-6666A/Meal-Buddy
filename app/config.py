from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "FanDazi API"
    # development | production。生产环境会在启动时强制检查安全配置。
    app_env: str = "development"
    database_url: str = "postgresql+psycopg://postgres:change_me@127.0.0.1:5432/fandazi"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_reasoning_effort: str = ""
    api_token: str = ""
    # 登录系统：管理员引导账号 + 邀请码注册 + 会话时长
    admin_username: str = "admin"
    admin_password: str = ""
    admin_email: str = ""  # 配了就绑到管理员账号，可用邮箱登录
    invite_code: str = ""
    session_ttl_days: int = 30
    # 邮箱验证码注册：配了 SMTP 发件账号即启用；未配置时回退为邀请码注册
    smtp_host: str = "smtp.qq.com"
    smtp_port: int = 465
    smtp_tls: str = "ssl"  # ssl | starttls | none
    smtp_user: str = ""  # 发件邮箱，例如你的 QQ 邮箱
    smtp_password: str = ""  # QQ 邮箱的 SMTP 授权码（不是 QQ 密码）
    email_from_name: str = "饭搭子"
    email_allowed_domains: str = ""  # 逗号分隔；空=不限，例如 qq.com
    email_code_ttl_minutes: int = 10
    email_code_resend_seconds: int = 60
    email_code_max_per_day: int = 10
    # 开发/测试免登录旁路；生产环境固定走 Cookie 会话
    dev_auto_login: bool = True
    rate_limit_per_minute: int = 10
    auth_rate_limit_per_minute: int = 5
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    amap_key: str = ""
    daily_token_budget: int = 0
    llm_price_per_1k_input: float = 0.0
    llm_price_per_1k_output: float = 0.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() in {"production", "prod"}

    def validate_for_startup(self) -> None:
        """生产环境缺关键配置时直接拒绝启动，避免无鉴权地暴露接口。"""
        if not self.is_production:
            return
        if not self.api_token:
            raise RuntimeError(
                "生产环境必须设置 API_TOKEN：否则导出、清理、删除接口会对任何能访问服务的人开放。"
            )
        if len(self.api_token) < 16:
            raise RuntimeError("生产环境的 API_TOKEN 至少 16 个字符。")
        if len(self.admin_password) < 8:
            raise RuntimeError(
                "生产环境必须设置 ADMIN_PASSWORD（至少 8 位）：用于首次启动引导管理员账号，"
                "历史数据会归入该账号名下。"
            )
        if "change_me" in self.database_url:
            raise RuntimeError("生产环境的 DATABASE_URL 仍是示例密码，请改成真实凭据。")


@lru_cache
def get_settings() -> Settings:
    return Settings()
