"""Telegram public channel scraper via t.me/s/ preview pages."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

from src.db.models import Source, SourceType
from src.sources.base import JobSource, Vacancy

TG_PREVIEW_BASE = "https://t.me/s"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


class TelegramChannelSource(JobSource):
    """Scrape recent posts from a public Telegram channel.

    source.identifier — channel username without '@' (e.g. 'normrabota').
    """

    def __init__(self, timeout: float = 15.0, max_posts: int = 30):
        self.timeout = timeout
        self.max_posts = max_posts

    async def fetch(self, source: Source) -> list[Vacancy]:
        username = source.identifier.lstrip("@").strip()
        url = f"{TG_PREVIEW_BASE}/{username}"

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
            if response.status_code != 200:
                return []
            html = response.text

        soup = BeautifulSoup(html, "html.parser")
        message_blocks = soup.select(".tgme_widget_message_wrap")

        vacancies: list[Vacancy] = []
        for block in message_blocks[-self.max_posts:]:
            vacancy = self._parse_post(block, username)
            if vacancy is not None:
                vacancies.append(vacancy)

        # Newest first
        vacancies.reverse()
        return vacancies

    def _parse_post(self, block, channel_username: str) -> Vacancy | None:
        msg = block.select_one(".tgme_widget_message")
        if msg is None:
            return None

        post_id = msg.get("data-post", "").strip()
        if not post_id:
            return None
        external_id = post_id.replace("/", "_")

        text_node = block.select_one(".tgme_widget_message_text")
        text = text_node.get_text("\n", strip=True) if text_node else ""

        if not text or len(text) < 50:
            return None

        # Filter posts that don't look like vacancies
        if not self._looks_like_vacancy(text):
            return None

        title = self._extract_title(text)
        company = self._extract_company(text)
        salary = self._extract_salary(text)
        location = self._extract_location(text)

        time_node = block.select_one(".tgme_widget_message_date time")
        published_at = time_node.get("datetime") if time_node else None

        post_url = f"https://t.me/{channel_username}/{post_id.split('/')[-1]}"

        return Vacancy(
            external_id=external_id,
            source_type=SourceType.telegram_channel,
            title=title,
            company=company,
            url=post_url,
            description=text[:4000],
            salary=salary,
            location=location,
            published_at=published_at,
            raw={"channel": channel_username, "text": text},
        )

    @staticmethod
    def _looks_like_vacancy(text: str) -> bool:
        """Heuristic: skip channel meta-posts, advertising, etc."""
        text_lower = text.lower()
        positive_markers = [
            "вакансия", "ищем", "ищу", "требуется", "позиция",
            "looking for", "we are hiring", "hiring", "ждём",
            "open role", "open position", "we're looking",
            "обязанности", "responsibilities", "требования", "что нужно",
            "зарплата", "salary", "от ", "компенсация", "вилка",
        ]
        return any(m in text_lower for m in positive_markers)

    @staticmethod
    def _extract_title(text: str) -> str:
        """Take first non-empty line as title."""
        for line in text.split("\n"):
            line = line.strip()
            if len(line) > 5:
                return line[:200]
        return "Вакансия"

    @staticmethod
    def _extract_company(text: str) -> str | None:
        """Heuristic: look for 'Company:' or '@username' or 'в COMPANY'."""
        patterns = [
            r"(?:компани[яи]|company)[:\s]+([A-Za-zА-Яа-яёЁ0-9\-\.&\s]{2,50})",
            r"в\s+([A-ZА-Я][A-Za-zА-Яа-я\-\.&]+(?:\s+[A-ZА-Я][A-Za-zА-Яа-я\-\.&]+){0,3})",
        ]
        for pat in patterns:
            match = re.search(pat, text)
            if match:
                return match.group(1).strip()[:100]
        return None

    @staticmethod
    def _extract_salary(text: str) -> str | None:
        patterns = [
            r"(\d{2,3}[\s,.]?\d{3}[\s,.]?\d{0,3})\s?[—–-]\s?(\d{2,3}[\s,.]?\d{3}[\s,.]?\d{0,3})\s?(?:руб|₽|RUB|usd|\$|€|eur)",
            r"(?:от|from)\s+(\d{2,3}[\s,.]?\d{3}[\s,.]?\d{0,3})\s?(?:руб|₽|RUB|usd|\$|€|eur)",
            r"(\d{2,3}[\s,.]?\d{3}[\s,.]?\d{0,3})\s?(?:руб|₽|RUB|usd|\$|€|eur)",
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                return match.group(0).strip()
        return None

    @staticmethod
    def _extract_location(text: str) -> str | None:
        keywords = {
            "удал": "удалённо",
            "remote": "удалённо",
            "москв": "Москва",
            "moscow": "Москва",
            "санкт-петербург": "Санкт-Петербург",
            "spb": "Санкт-Петербург",
            "тбилиси": "Тбилиси",
            "бишкек": "Бишкек",
            "ереван": "Ереван",
            "берлин": "Берлин",
            "лиссабон": "Лиссабон",
        }
        text_lower = text.lower()
        found = []
        for needle, label in keywords.items():
            if needle in text_lower and label not in found:
                found.append(label)
        return ", ".join(found) if found else None