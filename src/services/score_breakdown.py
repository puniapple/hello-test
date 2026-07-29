"""Score breakdown generation service.

Генерит структурированный разбор соответствия вакансии профилю через Sonnet 4.6.
Используется по клику на кнопку "Оценить совпадение" (Free-триал) или
"Показать совпадения" (Pro/Grandfather).

Промпт — из Сообщения_бота_28_07 (авторский стиль Ульяны, тёплый советчик).
Профиль передаётся в extra_system_blocks с cache_control — при нескольких
разборах подряд для одного юзера включается кеш (~5 мин TTL).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import structlog

from src.services.claude import ClaudeService
from src.sources.base import Vacancy

log = structlog.get_logger(__name__)

BREAKDOWN_MODEL = "claude-sonnet-4-6"

MAX_VACANCY_DESCRIPTION_CHARS = 4000

# Промпт взят из Сообщения_бота_28_07 (готовый, утверждённый)
BREAKDOWN_SYSTEM_PROMPT = """\
Ты — тёплый честный карьерный советчик в боте @FindFcknJobBot. \
Твоя задача — глядя на вакансию и профиль пользователя, объяснить ему, почему \
первичный матчер поставил именно такой скор соответствия, и помочь принять \
решение: тратить время на отклик или нет.

ТОН:
— Обращение на «ты», как к близкому человеку.
— Разговорный русский, без канцелярита. Не «данная позиция» — «эта вакансия». \
Не «кандидат должен» — «стоит обратить внимание».
— Голос — как у подружки-коуча, которая по-честному разбирает, не пытаясь ни продать, \
ни отговорить.
— Никакого HR-жаргона: не «соответствует хардам», не «покрывает грейд», не «синьор-миддл».
— Дроби фразы. Пиши короткими предложениями.

ЖЁСТКИЕ ПРАВИЛА ВЫВОДА:
— Возвращай ТОЛЬКО валидный JSON, без обёрток, комментариев и preamble.
— Структура: { "pros": [...], "gaps": [...], "verdict": "...", "verdict_level": "strong" | "worth" | "stretch" }
— pros: максимум 4 пункта. Что реально совпало между вакансией и профилем. \
Каждый пункт — одно короткое предложение. Только то, что явно есть в обоих текстах.
— gaps: максимум 3 пункта. На что пользователю стоит обратить внимание перед откликом — \
не хватает опыта, не покрыта важная зона, несовпадение по формату или условиям. \
Формулируй как совет, а не как приговор.
— verdict: одна короткая фраза, до 12 слов. Примеры хорошего вердикта:
  «Прям твоё — откликайся смело»
  «Стоит попробовать, если готов пойти на компромисс по формату»
  «С натяжкой, но если очень хочется — почему нет»
— verdict_level: "strong" (скор 8–10, минимум пробелов) / "worth" (скор 6–7 или есть \
значимые пробелы, но профиль в целом попадает) / "stretch" (скор 4–5, крупные несовпадения).

АНТИ-ГАЛЛЮЦИНАЦИЯ (КРИТИЧНО):
— НЕ придумывай требований, которых нет в тексте вакансии. Если в описании не сказано \
про удалёнку — не пиши «требуют офис».
— НЕ додумывай опыт пользователя, которого нет в профиле.
— Если в вакансии что-то не указано, а в профиле у пользователя это важный must-have — \
это gap («в вакансии не указано про X, уточни перед откликом»), а не факт.
— Пользователь принимает карьерное решение по твоему разбору. Ложный факт = вредный совет.

Сразу JSON. Не объясняй логику вне JSON. Не извиняйся.
"""


@dataclass
class BreakdownResult:
    """Результат генерации разбора."""
    pros: list[str]
    gaps: list[str]
    verdict: str
    verdict_level: str  # "strong" | "worth" | "stretch"
    model_used: str


def _build_profile_block(profile_data: dict) -> dict:
    """Профиль как отдельный system-блок с cache_control.

    При повторной генерации для того же юзера в течение 5 мин — cache hit
    на префиксе (промпт + профиль).
    """
    profile_json = json.dumps(profile_data, ensure_ascii=False, indent=2)
    return {
        "type": "text",
        "text": f"ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n{profile_json}",
        "cache_control": {"type": "ephemeral"},
    }


def _build_user_message(vacancy: Vacancy, haiku_score: float) -> str:
    description = (vacancy.description or "")[:MAX_VACANCY_DESCRIPTION_CHARS]
    return (
        f"ВАКАНСИЯ:\n"
        f"Название: {vacancy.title}\n"
        f"Компания: {vacancy.company or 'не указана'}\n"
        f"Локация: {vacancy.location or 'не указана'}\n"
        f"Зарплата: {vacancy.salary or 'не указана'}\n\n"
        f"Описание:\n{description}\n\n"
        f"СКОР ПЕРВИЧНОГО МАТЧЕРА: {haiku_score:.1f} из 10\n\n"
        f"Верни JSON с разбором."
    )


class ScoreBreakdownService:
    def __init__(self, claude: ClaudeService | None = None):
        self.claude = claude or ClaudeService(model=BREAKDOWN_MODEL)

    async def generate(
        self,
        profile_data: dict,
        vacancy: Vacancy,
        haiku_score: float,
    ) -> BreakdownResult:
        """Генерит разбор. Retry 1 раз при невалидном JSON.

        Кидает ValueError если оба attempt'а не смогли отдать валидный JSON.
        """
        user_message = _build_user_message(vacancy, haiku_score)
        profile_block = _build_profile_block(profile_data)

        last_raw = ""
        for attempt in range(2):
            response = await self.claude.chat(
                messages=[{"role": "user", "content": user_message}],
                system=BREAKDOWN_SYSTEM_PROMPT,
                extra_system_blocks=[profile_block],
                max_tokens=1024,
                model=BREAKDOWN_MODEL,
            )
            raw = (response.text or "").strip()
            last_raw = raw

            # Убираем возможные markdown-фенсы
            cleaned = re.sub(r"^```(?:json)?\s*", "", raw)
            cleaned = re.sub(r"\s*```$", "", cleaned)

            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError as e:
                log.warning(
                    "breakdown_json_parse_failed",
                    attempt=attempt,
                    error=str(e),
                    raw_preview=raw[:300],
                )
                continue

            # Валидация структуры
            pros = data.get("pros")
            gaps = data.get("gaps")
            verdict = data.get("verdict")
            verdict_level = data.get("verdict_level")

            if not isinstance(pros, list) or not isinstance(gaps, list):
                log.warning("breakdown_bad_structure", data=data)
                continue
            if not verdict or verdict_level not in ("strong", "worth", "stretch"):
                log.warning("breakdown_bad_verdict", data=data)
                continue

            return BreakdownResult(
                pros=[str(p) for p in pros[:4] if p],
                gaps=[str(g) for g in gaps[:3] if g],
                verdict=str(verdict),
                verdict_level=verdict_level,
                model_used=BREAKDOWN_MODEL,
            )

        raise ValueError(f"Sonnet returned invalid JSON twice: {last_raw[:200]}")