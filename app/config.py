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
    rate_limit_per_minute: int = 10
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
        if "change_me" in self.database_url:
            raise RuntimeError("生产环境的 DATABASE_URL 仍是示例密码，请改成真实凭据。")


@lru_cache
def get_settings() -> Settings:
    return Settings()
