"""hh.ru API source."""

from __future__ import annotations

import httpx

from src.db.models import Source, SourceType
from src.sources.base import JobSource, Vacancy

HH_API_BASE = "https://api.hh.ru"
DEFAULT_PER_PAGE = 50
USER_AGENT = "JobBot/0.1 (ulk.01172001@gmail.com)"


class HHSource(JobSource):
    """Fetcher for hh.ru public API. No auth needed for vacancy search."""

    def __init__(self, timeout: float = 15.0):
        self.timeout = timeout

    async def fetch(self, source: Source) -> list[Vacancy]:
        params = {
            "text": source.identifier,
            "per_page": DEFAULT_PER_PAGE,
            "order_by": "publication_time",
        }
        if source.filters:
            for key in ("area", "experience", "employment", "schedule", "salary"):
                value = source.filters.get(key)
                if value is not None:
                    params[key] = value

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{HH_API_BASE}/vacancies",
                params=params,
                headers={"User-Agent": USER_AGENT, "HH-User-Agent": USER_AGENT},
            )
            response.raise_for_status()
            payload = response.json()

        return [self._parse_item(item) for item in payload.get("items", [])]

    async def fetch_full_description(self, vacancy_id: str) -> str | None:
        """Get full HTML description (search endpoint returns only snippet)."""
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{HH_API_BASE}/vacancies/{vacancy_id}",
                headers={"User-Agent": USER_AGENT, "HH-User-Agent": USER_AGENT},
            )
            if response.status_code != 200:
                return None
            return response.json().get("description")

    def _parse_item(self, item: dict) -> Vacancy:
        salary = self._format_salary(item.get("salary"))
        snippet = item.get("snippet") or {}
        description = "\n".join(
            p for p in (snippet.get("requirement"), snippet.get("responsibility")) if p
        ).strip()

        employer = item.get("employer") or {}
        area = item.get("area") or {}

        return Vacancy(
            external_id=str(item["id"]),
            source_type=SourceType.hh_ru,
            title=item.get("name", "Без названия"),
            company=employer.get("name"),
            url=item.get("alternate_url") or item.get("url", ""),
            description=description,
            salary=salary,
            location=area.get("name"),
            published_at=item.get("published_at"),
            raw=item,
        )

    @staticmethod
    def _format_salary(salary: dict | None) -> str | None:
        if not salary:
            return None
        frm, to, currency = salary.get("from"), salary.get("to"), salary.get("currency") or ""
        if frm and to:
            return f"{frm}–{to} {currency}"
        if frm:
            return f"от {frm} {currency}"
        if to:
            return f"до {to} {currency}"
        return None