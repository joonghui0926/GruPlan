from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Gruplan AI"
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    data_go_kr_service_key: str | None = Field(default=None, alias="DATA_GO_KR_SERVICE_KEY")
    vworld_api_key: str | None = Field(default=None, alias="VWORLD_API_KEY")
    vworld_referer: str = Field(default="https://gruplan-ai.onrender.com", alias="VWORLD_REFERER")
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-5.4-mini", alias="OPENAI_MODEL")
    fire_risk_endpoint: str | None = Field(default=None, alias="FIRE_RISK_ENDPOINT")
    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")
    app_base_url: str = Field(default="https://gruplan-ai.onrender.com", alias="APP_BASE_URL")
    session_secret: str = Field(default="change-this-session-secret", alias="SESSION_SECRET")
    google_client_id: str | None = Field(default=None, alias="GOOGLE_CLIENT_ID")
    google_client_secret: str | None = Field(default=None, alias="GOOGLE_CLIENT_SECRET")
    kakao_client_id: str | None = Field(default=None, alias="KAKAO_CLIENT_ID")
    kakao_client_secret: str | None = Field(default=None, alias="KAKAO_CLIENT_SECRET")

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
