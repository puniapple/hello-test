"""Cheap text-based pre-filter to rank vacancies before sending to Haiku.

Goal: when fresh pool is 1000+ and matcher cap is 50, random shuffle
gives narrow-niche users nearly zero relevant vacancies. We instead
score each vacancy by lexical overlap with the user's profile keywords
and pick the top N — niche users see their relevant vacancies, broad
users see most-promising candidates first.

Also subtracts overlap with anti_roles/deal_breakers/industries_avoid,
so vacancies containing "product manager" for someone with anti-role
"продакт-менеджер" get pushed to the bottom, saving Haiku calls.
"""

from __future__ import annotations

import random
import re

from src.sources.base import Vacancy


# Profile fields that contain user's interests/roles/skills as text (positive signals).
_PROFILE_TEXT_FIELDS = (
    "target_roles",
    "expertise",
    "industries_interested",
    "interests_and_resonance",
    "ideal_work_description",
    "must_haves",
    "languages",
    "current_role_summary",
)

# Profile fields that contain what the user does NOT want (negative signals).
_PROFILE_ANTI_FIELDS = (
    "anti_roles",
    "deal_breakers",
    "industries_avoid",
)

# Russian + English stopwords frequent in profile text.
_STOPWORDS = {
    "и", "в", "на", "с", "по", "для", "не", "или", "что", "как",
    "это", "к", "из", "от", "за", "у", "о", "об", "при", "до",
    "the", "a", "an", "and", "or", "of", "in", "on", "at", "to",
    "for", "with", "by", "is", "are", "be", "as", "from",
}

_WORD_RE = re.compile(r"[a-zA-Zа-яА-ЯёЁ][a-zA-Zа-яА-ЯёЁ0-9+#.\-]{2,}")

# Штраф за anti-совпадение сильнее, чем награда за позитивное,
# чтобы явно нежелательные вакансии точно ушли в самый низ.
ANTI_WEIGHT = 3

# Порог отсечения. Вакансии с итоговым скором ниже — не идут в матчер.
# 1 означает: должно быть хотя бы одно pozitive-совпадение
# сверх любых anti-штрафов.
DEFAULT_MIN_SCORE = 1


def _tokenize(text: str) -> set[str]:
    return {
        w.lower()
        for w in _WORD_RE.findall(text or "")
        if w.lower() not in _STOPWORDS
    }


def _collect_field_text(profile_data: dict, fields: tuple[str, ...]) -> str:
    parts: list[str] = []
    for field in fields:
        val = profile_data.get(field)
        if not val:
            continue
        if isinstance(val, list):
            parts.extend(str(item) for item in val if item)
        elif isinstance(val, dict):
            parts.extend(str(v) for v in val.values() if v)
        else:
            parts.append(str(val))
    return " ".join(parts)


def _vacancy_text(v: Vacancy) -> str:
    bits = [v.title or "", v.company or "", v.description or "", v.location or ""]
    return " ".join(bits)


def rank_vacancies(
    vacancies: list[Vacancy],
    profile_data: dict,
    min_score: int = DEFAULT_MIN_SCORE,
) -> list[Vacancy]:
    """Return vacancies ordered by relevance to profile, descending.

    Score = positive overlap - anti overlap * ANTI_WEIGHT.
    Vacancies with score < min_score are dropped entirely (saves Haiku cost).

    If profile has no usable text at all, falls back to random order without dropping.
    """
    positive_tokens = _tokenize(_collect_field_text(profile_data, _PROFILE_TEXT_FIELDS))
    anti_tokens = _tokenize(_collect_field_text(profile_data, _PROFILE_ANTI_FIELDS))

    if not positive_tokens and not anti_tokens:
        # Профиль пустой по тексту — не отсекаем, просто перемешиваем.
        random.shuffle(vacancies)
        return vacancies

    scored: list[tuple[int, Vacancy]] = []
    for v in vacancies:
        vac_tokens = _tokenize(_vacancy_text(v))
        positive_overlap = len(positive_tokens & vac_tokens)
        anti_overlap = len(anti_tokens & vac_tokens)
        score = positive_overlap - anti_overlap * ANTI_WEIGHT
        scored.append((score, v))

    # Отсекаем всё, что ниже порога.
    scored = [(s, v) for s, v in scored if s >= min_score]

    # Перемешиваем внутри равных бакетов, чтобы не отдавать всегда одно и то же.
    random.shuffle(scored)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in scored]