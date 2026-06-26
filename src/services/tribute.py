"""Tribute Shop API client.

Все суммы — в минимальных единицах валюты (копейки для RUB, центы для EUR/USD).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any, Optional

import httpx
import structlog

from src.config import settings

log = structlog.get_logger(__name__)


class TributeError(Exception):
    """Любая ошибка при работе с Tribute API."""


class TributeClient:
    """Async-клиент для Tribute Shop API."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        shop_id: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        self.api_key = api_key or settings.tribute_api_key
        self.shop_id = shop_id or settings.tribute_shop_id
        self.base_url = (base_url or settings.tribute_base_url).rstrip("/")
        if not self.api_key:
            raise TributeError("TRIBUTE_API_KEY is not configured")
        self._client = httpx.AsyncClient(
            timeout=30.0,
            headers={"Api-Key": self.api_key, "Content-Type": "application/json"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    # --- Signature verification ---

    def verify_signature(self, raw_body: bytes, signature_header: str) -> bool:
        """Проверка HMAC-SHA256 подписи webhook'а.

        Подпись приходит в заголовке `trbt-signature`, ключ — наш API key.
        """
        if not signature_header:
            return False
        expected = hmac.new(
            self.api_key.encode("utf-8"),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature_header.strip())

    # --- Orders ---

    async def create_order(
        self,
        *,
        amount: int,
        currency: str,
        title: str,
        description: str,
        period: str = "monthly",
        customer_id: Optional[str] = None,
        success_url: Optional[str] = None,
        fail_url: Optional[str] = None,
        comment: Optional[str] = None,
    ) -> dict[str, Any]:
        """Создать заказ. Возвращает ShopOrder с paymentUrl и webappPaymentUrl."""
        body: dict[str, Any] = {
            "amount": amount,
            "currency": currency,
            "title": title[:100],
            "description": description[:300],
            "period": period,
        }
        if self.shop_id:
            try:
                body["shopId"] = int(self.shop_id)
            except (TypeError, ValueError):
                log.warning("invalid_shop_id", shop_id=self.shop_id)
        if customer_id:
            body["customerId"] = str(customer_id)[:256]
        if success_url:
            body["successUrl"] = success_url
        if fail_url:
            body["failUrl"] = fail_url
        if comment:
            body["comment"] = comment

        resp = await self._client.post(f"{self.base_url}/shop/orders", json=body)
        if resp.status_code != 200:
            log.error("tribute_create_order_failed", status=resp.status_code, body=resp.text)
            raise TributeError(f"create_order failed: {resp.status_code} {resp.text}")
        return resp.json()

    async def get_order(self, order_uuid: str) -> dict[str, Any]:
        resp = await self._client.get(f"{self.base_url}/shop/orders/{order_uuid}")
        if resp.status_code != 200:
            raise TributeError(f"get_order failed: {resp.status_code} {resp.text}")
        return resp.json()

    async def cancel_order(self, order_uuid: str) -> dict[str, Any]:
        """Отменить рекуррентную подписку. Доступ сохраняется до memberExpiresAt."""
        resp = await self._client.post(f"{self.base_url}/shop/orders/{order_uuid}/cancel")
        if resp.status_code != 200:
            raise TributeError(f"cancel_order failed: {resp.status_code} {resp.text}")
        return resp.json()

    async def get_shop(self) -> dict[str, Any]:
        params = {}
        if self.shop_id:
            try:
                params["shopId"] = int(self.shop_id)
            except (TypeError, ValueError):
                pass
        resp = await self._client.get(f"{self.base_url}/shop", params=params)
        if resp.status_code != 200:
            raise TributeError(f"get_shop failed: {resp.status_code} {resp.text}")
        return resp.json()


# --- Singleton для использования в хэндлерах ---

_client: Optional[TributeClient] = None


def get_tribute_client() -> TributeClient:
    global _client
    if _client is None:
        _client = TributeClient()
    return _client