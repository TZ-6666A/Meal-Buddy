from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "FanDazi API"
    database_url: str = "postgresql+psycopg://postgres:change_me@127.0.0.1:5432/fandazi"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    api_token: str = ""
    rate_limit_per_minute: int = 10
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    daily_token_budget: int = 0
    llm_price_per_1k_input: float = 0.0
    llm_price_per_1k_output: float = 0.0

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
