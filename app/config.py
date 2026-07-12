from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "MSL The Practice Media Insights"
    app_version: str = "0.3.0"
    environment: str = "development"

    app_password: str
    session_secret: str
    media_app_internal_secret: str

    database_url: str = "postgresql+psycopg://msl:msl@localhost:5432/msl_insights"

    session_cookie_name: str = "msl_session"
    session_max_age_seconds: int = 60 * 60 * 12  # 12 hours

    n8n_base_url: str = "https://n8n.aiexperiments.eu"
    n8n_classification_webhook_url: str = (
        "https://n8n.aiexperiments.eu/webhook/retail-media/classify"
    )
    n8n_narrative_webhook_url: str = (
        "https://n8n.aiexperiments.eu/webhook/retail-media/analyze"
    )
    n8n_chat_webhook_url: str = (
        "https://n8n.aiexperiments.eu/webhook/retail-media/chat"
    )
    n8n_request_timeout_seconds: float = 15.0

    upload_root_dir: str = "data/uploads"
    max_upload_size_bytes: int = 100 * 1024 * 1024  # 100 MB

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
