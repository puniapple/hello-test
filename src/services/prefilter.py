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


def _collect_profile_text(profile_data: dict) -> str:
    parts: list[str] = []
    for field in _PROFILE_TEXT_FIELDS:
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
) -> list[Vacancy]:
    """Return vacancies ordered by lexical overlap with profile, descending.

    Vacancies with zero overlap stay in the list (at the end, randomized
    among themselves) — Haiku may still find non-obvious matches there.
    """
    import random

    profile_tokens = _tokenize(_collect_profile_text(profile_data))
    if not profile_tokens:
        # Profile has no usable text — fall back to random order.
        random.shuffle(vacancies)
        return vacancies

    scored: list[tuple[int, Vacancy]] = []
    for v in vacancies:
        vac_tokens = _tokenize(_vacancy_text(v))
        overlap = len(profile_tokens & vac_tokens)
        scored.append((overlap, v))

    # Shuffle within equal-score groups so we don't always pick same vacancies
    random.shuffle(scored)
    scored.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in scored]