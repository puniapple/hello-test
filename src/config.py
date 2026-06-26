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

    # --- Tribute Shop API ---
    tribute_api_key: str = Field(default="")
    tribute_shop_id: str = Field(default="")
    tribute_base_url: str = Field(default="https://tribute.tg/api/v1")
    tribute_webhook_path: str = Field(default="/webhooks/tribute")

    # --- HTTP server для приёма webhook'ов ---
    webhook_host: str = Field(default="0.0.0.0")
    webhook_port: int = Field(default=8080)
    public_base_url: str = Field(default="")  # https://<railway-домен>.up.railway.app

    # --- Pricing Pro ---
    pro_price_kopeks: int = Field(default=99000)          # 990₽ подписка
    pro_onetime_price_kopeks: int = Field(default=99000)  # 990₽ разовая
    pro_period_recurring: str = Field(default="monthly")
    pro_period_onetime: str = Field(default="onetime")
    pro_onetime_days: int = Field(default=30)


settings = Settings()
