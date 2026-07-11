"""Cover letter generation service.

Генерит сопроводительное письмо на основе профиля юзера и одной вакансии.
Использует Sonnet (тот же, что для profile agent) — качество важнее скорости.
Язык письма определяется по языку вакансии.

Стоимость примерно $0.005-0.010 за письмо, зависит от длины резюме.
"""

from __future__ import annotations

import re

import structlog

from src.services.claude import ClaudeService
from src.sources.base import Vacancy

log = structlog.get_logger(__name__)

# Sonnet, потому что качество важно — юзер увидит текст и оценит.
COVER_LETTER_MODEL = "claude-sonnet-4-6"

MAX_VACANCY_DESCRIPTION_CHARS = 3000
MAX_CV_SUMMARY_CHARS = 2000


COVER_LETTER_SYSTEM_PROMPT = """\
Ты — эксперт по сопроводительным письмам. Твоя задача — написать письмо от имени \
конкретного человека под конкретную вакансию.

ЖЁСТКИЕ ПРАВИЛА:
1. Пиши на языке вакансии. Если вакансия на русском — письмо на русском. \
Если на английском — на английском.
2. Длина: 150-250 слов. Не короче, не длиннее. Юзер не сможет отправить \
"дорогая команда, я подхожу, спасибо", и его не будут читать письма на 500 слов.
3. Не льсти компании ("вы самая инновационная"), не пиши общих фраз \
("я энергичный командный игрок"). Пиши конкретно: что человек сделал раньше \
и почему это релевантно этой роли.
4. Используй ФАКТЫ из профиля и резюме — цифры, компании, роли. Не выдумывай.
5. Не начинай с "меня зовут X" — это в подписи. Начинай с крючка: почему \
эта роль/компания.
6. Не пиши "прикладываю резюме" — юзер отправит письмо через форму, там резюме \
уже приложено отдельно.
7. Никаких placeholder'ов типа [Имя] или [название компании] — если данных \
нет, обходи их.
8. Тон: уверенный, профессиональный, живой. Не пафос, не мольба.

СТРУКТУРА:
- Первый абзац (2-3 предложения): почему эта роль/компания зацепила, \
что общего с текущим опытом.
- Второй абзац (3-4 предложения): 1-2 конкретных факта из опыта, релевантных \
задачам роли. С цифрами, если есть.
- Третий абзац (1-2 предложения): что хочу обсудить на созвоне / \
почему стоит поговорить.

Ответь только текстом письма, без преамбулы, без markdown, без "Sincerely,". \
Подпись имени не добавляй — юзер добавит сам.
"""


def _detect_language(vacancy: Vacancy) -> str:
    """Определяет язык вакансии — 'ru' или 'en'.

    Простой эвристический детектор по доле кириллицы в title + description.
    """
    text = f"{vacancy.title or ''} {vacancy.description or ''}"
    if not text.strip():
        return "en"

    cyrillic_count = sum(1 for c in text if "а" <= c.lower() <= "я" or c.lower() == "ё")
    latin_count = sum(1 for c in text if "a" <= c.lower() <= "z")

    if cyrillic_count == 0 and latin_count == 0:
        return "en"
    return "ru" if cyrillic_count > latin_count else "en"


def _extract_cv_summary(profile_data: dict, max_sources: int = 3) -> str:
    """Достаёт summary из первых N резюме юзера.

    cv_sources в profile_data — список dict с полями filename, uploaded_at, summary_extracted.
    """
    cv_sources = profile_data.get("cv_sources") or []
    if not isinstance(cv_sources, list):
        return ""

    summaries: list[str] = []
    for cv in cv_sources[:max_sources]:
        if not isinstance(cv, dict):
            continue
        summary = cv.get("summary_extracted") or ""
        if summary:
            summaries.append(str(summary))

    combined = "\n\n---\n\n".join(summaries)
    return combined[:MAX_CV_SUMMARY_CHARS]


def _build_user_message(
    vacancy: Vacancy,
    profile_data: dict,
    language: str,
) -> str:
    description = (vacancy.description or "")[:MAX_VACANCY_DESCRIPTION_CHARS]
    cv_summary = _extract_cv_summary(profile_data)

    profile_bits: list[str] = []
    for field in ("expertise", "target_roles", "current_role_summary",
                  "interests_and_resonance", "ideal_work_description"):
        val = profile_data.get(field)
        if not val:
            continue
        if isinstance(val, list):
            val = ", ".join(str(x) for x in val if x)
        elif isinstance(val, dict):
            val = ", ".join(str(v) for v in val.values() if v)
        profile_bits.append(f"{field}: {val}")

    profile_block = "\n".join(profile_bits) if profile_bits else "(профиль пустой)"

    lang_hint = "Пиши письмо на русском." if language == "ru" else "Write the letter in English."

    return f"""ПРОФИЛЬ ЧЕЛОВЕКА:
{profile_block}

РЕЗЮМЕ (краткое содержание):
{cv_summary if cv_summary else "(резюме не загружено — опирайся только на профиль)"}

ВАКАНСИЯ:
Название: {vacancy.title}
Компания: {vacancy.company or "не указана"}
Локация: {vacancy.location or "не указана"}
Зарплата: {vacancy.salary or "не указана"}
Описание:
{description}

{lang_hint} Верни только текст письма."""


class CoverLetterService:
    def __init__(self, claude: ClaudeService | None = None):
        self.claude = claude or ClaudeService(model=COVER_LETTER_MODEL)

    async def generate(self, profile_data: dict, vacancy: Vacancy) -> str:
        """Возвращает готовый текст сопроводительного письма.

        Кидает исключение, если Claude не ответил или ответ пустой.
        """
        language = _detect_language(vacancy)
        user_message = _build_user_message(vacancy, profile_data, language)

        response = await self.claude.chat(
            messages=[{"role": "user", "content": user_message}],
            system=COVER_LETTER_SYSTEM_PROMPT,
            max_tokens=1024,
            model=COVER_LETTER_MODEL,
        )

        text = (response.text or "").strip()
        if not text:
            raise ValueError("Claude returned empty cover letter")

        # Убираем markdown фенсы если Claude их случайно добавил
        text = re.sub(r"^```(?:markdown|text)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

        return text.strip()