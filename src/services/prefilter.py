"""Cheap text-based pre-filter to rank vacancies before sending to Haiku.

Goal: when fresh pool is 1000+ and matcher cap is 50, random shuffle
gives narrow-niche users nearly zero relevant vacancies. We instead
score each vacancy by lexical overlap with the user's profile keywords
and pick the top N — niche users see their relevant vacancies, broad
users see most-promising candidates first.
"""

from __future__ import annotations

import re
from src.sources.base import Vacancy

# Profile fields that contain user's interests/roles/skills as text.
# Each can be str or list[str] in the JSONB profile_data.
_PROFILE_TEXT_FIELDS = (
    "target_roles",
    "expertise",
    "industries_interested",
    "interests_and_resonance",
    "ideal_work_description",
    "must_haves",
    "languages",
)

# Weights per field. Higher = more important for matching.
_FIELD_WEIGHTS = {
    "target_roles": 3,
    "expertise": 2,
    "industries_interested": 1,
    "interests_and_resonance": 1,
    "ideal_work_description": 1,
    "must_haves": 1,
    "languages": 1,
}

# Fields that describe what user does NOT want.
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


def _tokenize(text: str) -> set[str]:
    return {
        w.lower()
        for w in _WORD_RE.findall(text or "")
        if w.lower() not in _STOPWORDS
    }


def _collect_profile_text(profile_data: dict, fields: tuple) -> str:
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

def _collect_weighted_tokens(profile_data: dict) -> dict[str, int]:
    """Return {token: weight} where weight is per-field importance.

    If token appears in multiple fields, we take the maximum weight.
    """
    weighted: dict[str, int] = {}
    for field, weight in _FIELD_WEIGHTS.items():
        val = profile_data.get(field)
        if not val:
            continue
        if isinstance(val, list):
            text = " ".join(str(item) for item in val if item)
        elif isinstance(val, dict):
            text = " ".join(str(v) for v in val.values() if v)
        else:
            text = str(val)
        for token in _tokenize(text):
            weighted[token] = max(weighted.get(token, 0), weight)
    return weighted

def _vacancy_text(v: Vacancy) -> str:
    bits = [v.title or "", v.company or "", v.description or "", v.location or ""]
    return " ".join(bits)


def rank_vacancies(
    vacancies: list[Vacancy],
    profile_data: dict,
) -> list[Vacancy]:
    """Return vacancies ordered by lexical overlap with profile, descending.

    Vacancies with zero overlap stay in the list (at the end, randomized
    among themselves) — Haiku may still find non-obvious matches there.
    """
    import random

    positive_weighted = _collect_weighted_tokens(profile_data)
    negative_tokens = _tokenize(_collect_profile_text(profile_data, _PROFILE_ANTI_FIELDS))
    if not positive_weighted:
        # Profile has no usable text — fall back to random order.
        random.shuffle(vacancies)
        return vacancies

    scored: list[tuple[int, Vacancy]] = []
    for v in vacancies:
        vac_tokens = _tokenize(_vacancy_text(v))
        # Sum weights for tokens that appear in vacancy
        positive = sum(w for t, w in positive_weighted.items() if t in vac_tokens)
        negative = len(negative_tokens & vac_tokens)
        # Штраф за anti-поля вдвое сильнее чем каждый совпавший вес positive
        overlap = positive - negative * 2
        scored.append((overlap, v))

    # Shuffle within equal-score groups so we don't always pick same vacancies
    random.shuffle(scored)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in scored]