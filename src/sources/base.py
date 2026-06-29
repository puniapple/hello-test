"""Base classes for vacancy sources."""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.db.models import Source, SourceType


@dataclass
class Vacancy:
    """Unified representation of a job posting from any source."""

    external_id: str
    source_type: SourceType
    title: str
    company: str | None
    url: str
    description: str
    salary: str | None = None
    location: str | None = None
    published_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def hash(self) -> str:
        """Stable identifier for source-specific dedup."""
        key = f"{self.source_type.value}:{self.external_id}"
        return hashlib.sha256(key.encode()).hexdigest()

    @property
    def content_fingerprint(self) -> str:
        """Cross-source fingerprint: same text from different channels matches."""
        from src.services.dedup import compute_content_fingerprint
        return compute_content_fingerprint(self.title, self.company, self.description)

    @property
    def global_external_id(self) -> str | None:
        """Job-platform URL if any was found in description (hh.ru, lever, etc.)."""
        from src.services.dedup import extract_global_external_id
        # Сначала смотрим в описании, потом в самой URL вакансии
        candidate = extract_global_external_id(self.description) or extract_global_external_id(self.url)
        return candidate

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "source_type": self.source_type.value,
            "title": self.title,
            "company": self.company,
            "url": self.url,
            "description": self.description,
            "salary": self.salary,
            "location": self.location,
            "published_at": self.published_at,
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "Vacancy":
        """Восстановить Vacancy из dict, сохранённого через to_storage_dict."""
        source_type = data["source_type"]
        if isinstance(source_type, str):
            source_type = SourceType(source_type)
        return cls(
            external_id=data["external_id"],
            source_type=source_type,
            title=data["title"],
            company=data.get("company"),
            url=data["url"],
            description=data["description"],
            salary=data.get("salary"),
            location=data.get("location"),
            published_at=data.get("published_at"),
            raw=data.get("raw", {}),
        )


class JobSource(ABC):
    """Abstract base for any source of vacancies."""

    @abstractmethod
    async def fetch(self, source: Source) -> list[Vacancy]:
        """Fetch a fresh batch of vacancies for a given source row.

        Returns newest-first. Deduplication is the caller's responsibility.
        """
        ...