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

    # --- Tribute (subscriptions API) ---
    tribute_api_key: str = Field(default="")
    tribute_base_url: str = Field(default="https://tribute.tg/api/v1")
    tribute_webhook_path: str = Field(default="/webhooks/tribute")

    # URL страницы оплаты месячной подписки в Tribute (получаешь после создания подписки в дашборде)
    # Пример: https://t.me/tribute/app?startapp=sZWl
    tribute_subscription_monthly_url: str = Field(default="")
    # URL страницы оплаты недельной подписки в Tribute (получаешь после создания подписки в дашборде)
    tribute_subscription_weekly_url: str = Field(default="")

    # Username канала-гейта (без @). Юзер после оплаты попадёт в этот канал автоматически.
    tribute_channel_username: str = Field(default="puniapple_findjob")

    # --- HTTP server для приёма webhook'ов ---
    webhook_host: str = Field(default="0.0.0.0")
    webhook_port: int = Field(default=8080)
    public_base_url: str = Field(default="")

    # --- Pricing Pro ---
    pro_price_rub: int = Field(default=990)


settings = Settings()
