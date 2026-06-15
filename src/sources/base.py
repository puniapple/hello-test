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
        """Stable identifier for deduplication."""
        key = f"{self.source_type.value}:{self.external_id}"
        return hashlib.sha256(key.encode()).hexdigest()

    def to_storage_dict(self) -> dict[str, Any]:
        """Serializable form for storing in vacancy_matches.vacancy_data."""
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


class JobSource(ABC):
    """Abstract base for any source of vacancies."""

    @abstractmethod
    async def fetch(self, source: Source) -> list[Vacancy]:
        """Fetch a fresh batch of vacancies for a given source row.

        Returns newest-first. Deduplication is the caller's responsibility.
        """
        ...