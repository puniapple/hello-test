"""Quick smoke test for matcher cache — без Telegram."""
import asyncio
from src.agents.matcher import VacancyMatcher
from src.db.models import SourceType
from dataclasses import dataclass


@dataclass
class FakeVacancy:
    external_id: str
    source_type: SourceType
    title: str
    company: str
    url: str
    description: str
    salary: str | None
    location: str | None
    published_at: None
    raw: dict


async def main():
    profile = {
        "expertise": "Business Development",
        "target_roles": ["BD", "Growth"],
        "seniority": "senior",
        "languages": {"ru": "native", "en": "B2"},
    }
    
    vacancy = FakeVacancy(
        external_id="test:1",
        source_type=SourceType.career_site,
        title="Senior Business Development Manager",
        company="TestCo",
        url="https://example.com",
        description="Looking for experienced BD with growth marketing skills",
        salary="$5000",
        location="Remote",
        published_at=None,
        raw={},
    )

    matcher = VacancyMatcher()
    
    # Прогоняем 3 раза подряд — должен быть cache hit на 2-м и 3-м
    for i in range(3):
        result = await matcher.match(profile, vacancy)
        print(f"\n=== Call {i+1} ===")
        print(f"Score: {result.score}")
        print(f"Reason: {result.fit_reason[:200]}")
        print(f"Should send: {result.should_send}")


asyncio.run(main())