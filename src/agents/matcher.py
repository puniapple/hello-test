"""Vacancy matcher: scores how well a vacancy fits a user's profile."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from src.services.claude import ClaudeService
from src.sources.base import Vacancy

logger = logging.getLogger(__name__)

MATCHER_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_SCORE_THRESHOLD = 5.0
MAX_DESCRIPTION_CHARS = 2500

MATCHER_SYSTEM_PROMPT = """\
Ты — карьерный матчер. Тебе дают профиль человека и одну вакансию. \
Твоя задача — оценить, насколько вакансия подходит этому человеку, по шкале 0-10.

ШКАЛА (применяй её строго):
- 9-10: прямое попадание в target_roles + сильное совпадение по expertise + резонирует с industries_interested или interests_and_resonance + нет красных флагов
- 7-8: либо прямая target_role с одним некритичным минусом, либо роль из соседней области (близкая к target), но в правильной индустрии и с правильным уровнем
- 5-6: пограничная зона — есть совпадения по части критериев, но также есть значимые несоответствия. Релевантно посмотреть, но не приоритет
- 3-4: вакансия в anti_roles ИЛИ срабатывает один deal_breaker ИЛИ зарплата сильно ниже min_monthly. Даже если другие критерии совпадают — скор не выше 4.
- 0-2: вакансия противоречит сразу нескольким полям профиля, явно "не для этого человека"

ЖЁСТКИЕ ПРАВИЛА (НЕ НАРУШАЙ):
1. Если роль вакансии явно попадает в anti_roles (например, в anti_roles написано "продакт-менеджер", а вакансия — Product Manager / PM / CPO / Senior Product Manager) — МАКСИМАЛЬНЫЙ скор 4.0, независимо от других совпадений. Не пиши "PM, но adjacent" — если это PM, это PM.
2. Если срабатывает хотя бы один deal_breaker — МАКСИМАЛЬНЫЙ скор 4.0.
3. Если зарплата указана и она ниже min_monthly юзера — МАКСИМАЛЬНЫЙ скор 4.0.
4. Если вакансия попадает в industries_avoid — МАКСИМАЛЬНЫЙ скор 4.0.
5. Эти правила работают как срезающие потолки, не складываются. Один сработавший — потолок 4.0, точка.

КАК ЧИТАТЬ ANTI-ROLES:
- "продакт-менеджер" в anti_roles означает, что любая роль с названием Product Manager / PM / Product Owner / CPO / Head of Product — это anti-role, даже если вакансия "не совсем такой PM" или "стратегический PM". Юзер сам решил не идти в этом направлении — твоя задача уважать это, а не уговаривать.
- Различай: "не хочу быть PM" ≠ "не хочу работать с продуктом". Юзер может работать с продуктом через BD/Growth/Strategy — но не как PM.

КАК ЧИТАТЬ TARGET-ROLES:
- target_roles содержит конкретные позиции, на которые юзер хочет. Если вакансия по названию и сути попадает — это +2 балла к базе.
- Соседние области (например, Growth Lead vs Growth Manager) — тоже считаются target, если функционально совпадают.

КАК ЧИТАТЬ INTERESTS_AND_RESONANCE:
- Это "лежит душа", индустрия или сфера. Совпадение даёт +1 балл, но не делает не-target роль target'ом.
- EdTech-компания, ищущая PM — это всё равно PM (anti-role), потолок 4.0.
- EdTech-компания, ищущая Growth Manager — это target_role + резонирующая индустрия, скор 9-10.

ЕСЛИ ДЕТАЛИ НЕ УКАЗАНЫ:
- Нет зарплаты → не снижай скор, но добавь "зарплата не указана" в red_flags
- Нет локации → попробуй догадаться по компании/контексту, иначе нейтрально
- Минимальное описание → оцени по тому, что есть, не выдумывай

Ответ строго в JSON, БЕЗ markdown, БЕЗ преамбулы:
{
  "score": число от 0 до 10 (можно с десятыми, например 7.5),
  "fit_reason": "одно предложение на русском — почему именно такой скор",
  "red_flags": ["короткие", "формулировки"] или [],
  "should_send": true/false
}

should_send=true если score >= 5.0.
"""


@dataclass
class MatchResult:
    score: float
    fit_reason: str
    red_flags: list[str]
    should_send: bool


class VacancyMatcher:
    def __init__(self, claude: ClaudeService | None = None, threshold: float = DEFAULT_SCORE_THRESHOLD):
        self.claude = claude or ClaudeService(model=MATCHER_MODEL)
        self.threshold = threshold

    async def match(self, profile_data: dict, vacancy: Vacancy) -> MatchResult:
        user_message = self._build_user_message(profile_data, vacancy)

        for attempt in range(2):
            response = await self.claude.chat(
                messages=[{"role": "user", "content": user_message}],
                system=MATCHER_SYSTEM_PROMPT,
                max_tokens=512,
                model=MATCHER_MODEL,
            )
            parsed = self._parse_response(response.text)
            if parsed is not None:
                return parsed
            logger.warning(
                "matcher_parse_failed",
                extra={"attempt": attempt, "response_text": response.text[:300]},
            )

        # Both attempts failed — conservative fallback
        return MatchResult(
            score=0.0,
            fit_reason="Не удалось оценить (ошибка модели)",
            red_flags=["matcher_error"],
            should_send=False,
        )

    def _build_user_message(self, profile_data: dict, vacancy: Vacancy) -> str:
        profile_json = json.dumps(profile_data, ensure_ascii=False, indent=2)
        description = (vacancy.description or "")[:MAX_DESCRIPTION_CHARS]
        return (
            f"ПРОФИЛЬ ЮЗЕРА:\n{profile_json}\n\n"
            f"ВАКАНСИЯ:\n"
            f"Название: {vacancy.title}\n"
            f"Компания: {vacancy.company or 'не указана'}\n"
            f"Локация: {vacancy.location or 'не указана'}\n"
            f"Зарплата: {vacancy.salary or 'не указана'}\n"
            f"Источник: {vacancy.source_type.value}\n"
            f"Описание:\n{description}\n\n"
            f"Оцени соответствие. Верни JSON."
        )

    def _parse_response(self, text: str) -> MatchResult | None:
        if not text:
            return None
        # Иногда Claude добавляет ```json фенсы, несмотря на инструкцию
        clean = text.strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*", "", clean)
            clean = re.sub(r"\s*```$", "", clean)

        try:
            data = json.loads(clean)
        except json.JSONDecodeError:
            return None

        try:
            score = float(data.get("score", 0))
            fit_reason = str(data.get("fit_reason", ""))[:500]
            red_flags = list(data.get("red_flags") or [])[:10]
            red_flags = [str(rf)[:100] for rf in red_flags]
            should_send = bool(data.get("should_send", score >= self.threshold))
        except (ValueError, TypeError):
            return None

        return MatchResult(
            score=score,
            fit_reason=fit_reason,
            red_flags=red_flags,
            should_send=should_send,
        )