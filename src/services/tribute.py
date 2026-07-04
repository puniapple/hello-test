"""Tribute API client — только signature verification для webhook'ов.

Управление подписками (создание, отмена) делается автором в дашборде Tribute вручную,
не через API. Здесь нам нужна только проверка подписи входящих webhook'ов.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Optional

import structlog

from src.config import settings

log = structlog.get_logger(__name__)


class TributeError(Exception):
    """Ошибки при работе с Tribute."""


class TributeClient:
    """Клиент Tribute — сейчас только для проверки подписи webhook'ов.

    В будущем сюда можно будет добавить API-вызовы если Tribute расширит функционал
    подписок (например, получение статистики по подпискам через API).
    """

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or settings.tribute_api_key
        if not self.api_key:
            raise TributeError("TRIBUTE_API_KEY is not configured")

    def verify_signature(self, raw_body: bytes, signature_header: str) -> bool:
        """Проверка HMAC-SHA256 подписи webhook'а.

        Tribute шлёт подпись в заголовке `trbt-signature`, ключ — наш API key.
        """
        if not signature_header:
            return False
        expected = hmac.new(
            self.api_key.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header.strip())


# --- Singleton для использования в webhook handler ---

_client: Optional[TributeClient] = None


def get_tribute_client() -> TributeClient:
    global _client
    if _client is None:
        _client = TributeClient()
    return _client