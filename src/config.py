"""Application configuration loaded from environment."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str = Field(default="")
    anthropic_api_key: str = Field(default="")
    openai_api_key: str = Field(default="")
    database_url: str = Field(
        default="postgresql+asyncpg://jobbot:jobbot_dev_password@localhost:5432/jobbot"
    )
    environment: str = Field(default="dev")
    log_level: str = Field(default="INFO")

    admin_telegram_ids: str = ""  # comma-separated list of admin Telegram IDs
    required_channel_username: str = ""  # например "@your_channel", пусто = выключено


settings = Settings()
