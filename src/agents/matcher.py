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
DEFAULT_SCORE_THRESHOLD = 4.5
MAX_DESCRIPTION_CHARS = 2500

MATCHER_SYSTEM_PROMPT = """\
Ты — карьерный матчер. Тебе дают профиль человека и одну вакансию. Твоя задача — оценить, насколько вакансия подходит этому человеку, по шкале 0-10.

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

КАК ЧИТАТЬ ANTI-ROLES — ВАЖНО:
- Anti-roles бывают ДВУХ типов:
  (а) По названию: "продакт-менеджер", "team lead" — сверяй с job title вакансии.
  (б) По ХАРАКТЕРУ ЗАДАЧ: "поддержка legacy без новых задач", "рутинный дебаггинг", "team lead pressure", "холодные звонки". Их НЕ проверить по названию — читай описание вакансии.
- Пример по типу (б): если у юзера anti_role = "поддержка legacy", а вакансия "SDK Engineer" — читай что делает SDK Engineer. Если описание содержит "maintain existing SDK", "fix production issues", "работа с существующей кодовой базой" — это ЗАДЕВАЕТ anti-role, даже если название "Engineer". Потолок 5.0.
- Различай: "не хочу быть PM" ≠ "не хочу работать с продуктом". Юзер может работать с продуктом через BD/Growth/Strategy — но не как PM.

КАК ЧИТАТЬ TARGET-ROLES:
- target_roles содержит конкретные позиции, на которые юзер хочет. Если вакансия по названию и сути попадает — это +2 балла к базе.
- Соседние области (например, Growth Lead vs Growth Manager) — тоже считаются target, если функционально совпадают.

КАК ЧИТАТЬ РОЛИ, КОТОРЫХ НЕТ НИ В TARGET, НИ В ANTI:
- Если роль вакансии не в target_roles и не в anti_roles — это НЕ автоматически "соседняя область". Это либо соседняя, либо чужая.
- Соседняя = функционально пересекается (не тематически, а по ЕЖЕДНЕВНЫМ ЗАДАЧАМ). Growth Manager и Growth Lead — соседние, потому что 80% задач те же. Growth Manager и Data Analyst — НЕ соседние, потому что задачи разные: один запускает эксперименты и растит метрики, другой строит модели и делает отчёты. То, что оба работают "с метриками" и "с продуктом" — это тематика, а не функция.
- Если сомневаешься — соседняя роль или чужая — задай себе вопрос: "Может ли этот человек прийти на эту позицию без переквалификации?" BD с growth-опытом может прийти на Growth Manager без переквалификации. На Product Analyst — нет, там другой инструментарий, другой день.
- Чужая роль, даже с большим тематическим пересечением, максимальный скор — 5.0. Не 7, не 6.5. Пять.

ФУНКЦИЯ ПРОТИВ ТЕМЫ:
- Тема = что за индустрия/продукт/сфера (EdTech, FinTech, аналитика, growth).
- Функция = что человек делает каждый день (запускает эксперименты / строит модели / ведёт переговоры / пишет код).
- В матчинге ФУНКЦИЯ важнее ТЕМЫ. BizDev в EdTech и Product Analyst в EdTech — совсем разные роли, хотя тема одна.
- Резонирующая тема без совпадающей функции — это +0.5 к базе, не +2.

КАК ЧИТАТЬ INTERESTS_AND_RESONANCE:
- Это "лежит душа", индустрия или сфера. Совпадение даёт +1 балл, но не делает не-target роль target'ом.
- EdTech-компания, ищущая PM — это всё равно PM (anti-role), потолок 4.0.
- EdTech-компания, ищущая Growth Manager — это target_role + резонирующая индустрия, скор 9-10.

КАК ИСПОЛЬЗОВАТЬ CURRENT_ROLE_SUMMARY:
- Это описание того, чем человек занимается СЕЙЧАС. Это якорь его текущей функции.
- Если вакансия функционально далеко от current_role_summary И роль не в target_roles — это чужая роль, потолок 5.0.
- Пример: current_role_summary говорит "senior BD, партнёрства и B2B продажи", вакансия — Data Scientist. Даже если у человека в expertise есть "SQL" и "аналитика" — это чужая роль. Потолок 5.

SENIORITY (НОВЫЙ БЛОК — ВАЖНО):
- Сравни seniority профиля и seniority вакансии.
- Соответствие в диапазоне ±1 уровень — норм (Senior юзер на Senior/Lead позицию — ок).
- ДАУНГРЕЙД на 2+ уровня — потолок 6.0. Head/Director юзер на Senior позицию — это шаг назад, даже если функция подходит.
- Примеры даунгрейда:
  * Head of Anti-Fraud (15 лет, управлял командой 10+) → Senior Risk Strategist (индивидуальный контрибьютор). Потолок 6.0.
  * Product Marketing Lead (7 лет) → Product Marketing Specialist (без команды). Потолок 6.0.
- АПГРЕЙД на 2+ уровня без соответствующего опыта — потолок 5.5. Junior юзер на "Head of X" программу — нереалистично.
  * Студент/Junior → "CEO Fast Track" / "Leadership Program" в крупной корпорации. Даже если стажировка — это программа для будущих руководителей, требует MBA/лидерского бэкграунда. Потолок 5.0.

МЕЖДУНАРОДНЫЕ ВАКАНСИИ И ЛОКАЦИЯ (НОВЫЙ БЛОК):
- Прочитай location_preferences юзера ВНИМАТЕЛЬНО.
- Если юзер указал конкретные страны (РФ, ex-USSR, СНГ) БЕЗ явного "готов к релокации":
  * Вакансия в US/EU без remote-worldwide и без релокационного пакета → потолок 6.0
  * Вакансия remote-worldwide (любая страна) → нейтрально, оценивай по остальным критериям
  * Вакансия в US/EU С явно указанной релокацией и визой → +1 балл, если релокация в profile ok
- Если у юзера remote_ok=True и нет ограничения по странам — международные без релокации оцениваются нейтрально.
- Даже если функция и роль идеально подходят — при location mismatch без релокации потолок 6.0. Это не отказ (юзер может передумать), но и не приоритет.

ЕСЛИ ДЕТАЛИ НЕ УКАЗАНЫ:
- Нет зарплаты → не снижай скор, но добавь "зарплата не указана" в red_flags
- Нет локации → попробуй догадаться по компании/контексту, иначе нейтрально
- Минимальное описание → оцени по тому, что есть, не выдумывай

────────────────────────────────────────
ПРИМЕРЫ (учись на них):
────────────────────────────────────────

ПРИМЕР 1 — прямое попадание (скор 9.5):
Профиль: Senior Backend Developer, expertise=[Kotlin, Java], target=[Senior Backend Developer], seniority=Senior, industries_interested=[финтех]
Вакансия: "Senior Kotlin разработчик" в Affirm, финтех, зарплата 5.8M ₽, remote
Скор: 9.5
Почему: Прямое совпадение target_role, стек, индустрия резонирует, зарплата хорошая, remote ок. Красных флагов нет.

ПРИМЕР 2 — прямое попадание в узкую нишу (скор 9.0):
Профиль: expertise=[Проектирование дорог, Управление отделом проектирования], target=[Руководитель проекта транспортной инфраструктуры], seniority=Senior/Lead 15+ лет
Вакансия: "Руководитель проекта" в девелоперской компании (жилая недвижимость)
Скор: 6.0
Почему: Название совпадает с target, но DOMAIN другой — жилая недвижимость vs транспортная инфраструктура. Это функция другого домена, потолок 6.0 из-за domain mismatch. НЕ 8.5.

ПРИМЕР 3 — anti-role по характеру задач (скор 5.0):
Профиль: Senior Android Developer, target=[Senior Android Developer, Mobile Developer], anti_roles=["поддержка legacy без новых задач", "рутинный дебаггинг"]
Вакансия: "SDK Engineer - Kotlin" — "maintain existing SDK, resolve production issues, refactor legacy modules"
Скор: 5.0
Почему: Название "Engineer" звучит как target, но описание — чистый поддерживающий SDK-инжиниринг с багфиксами и legacy. Задевает anti-role по типу (б) — характер задач. Потолок 5.0.

ПРИМЕР 4 — seniority downgrade (скор 6.0):
Профиль: Head of Anti-Fraud (15+ лет, управлял командой 10+), target=[Head of Anti-Fraud, Head of Risk], seniority=Head/Senior Lead
Вакансия: "Senior Risk Strategist - Fraud" в fintech (индивидуальный контрибьютор, без команды)
Скор: 6.0
Почему: Target-role по домену (fraud, risk), но это Senior позиция — минус два уровня от Head. Даунгрейд. Fintech-тема резонирует, но карьерно это шаг назад. Потолок 6.0.

ПРИМЕР 5 — международная без релокации (скор 6.0):
Профиль: Product Marketing Manager, target=[Продуктовый маркетолог], seniority=middle, location_preferences={countries: [РФ, СНГ]}, remote_ok=True без явной готовности к переезду
Вакансия: "Product Marketing - Agents" в Stripe, HQ в US, remote но с ограничением по часовым поясам US/EU
Скор: 6.0
Почему: Роль идеально совпадает, компания топ, но location — US/EU-only remote. У юзера в location_preferences нет US, готовность к релокации не указана. Потолок 6.0.

ПРИМЕР 6 — junior на leadership программу (скор 5.0):
Профиль: студент последнего курса, target=[Бизнес-ассистент, Помощник руководителя], seniority=Junior/Stажёр
Вакансия: "Стажёр CEO Fast Track Программа" в Магните (2 года ротаций, менторство от топ-менеджеров, ожидается что после программы возглавит направление)
Скор: 5.0
Почему: Название "стажёр" вводит в заблуждение. По сути — Leadership Development Program для будущих директоров, требует лидерского бэкграунда или MBA. Юзер junior без управленческого опыта — не пройдёт первый этап отбора. Потолок 5.0.

ПРИМЕР 7 — правильный international match (скор 9.0):
Профиль: Senior Android Developer, expertise=[Kotlin, Jetpack Compose], target=[Senior Android Developer], location_preferences={"вне РФ", remote_ok=True}, готов к релокации
Вакансия: "Android Engineer, Terminal" в Stripe, remote + релокация возможна, финтех
Скор: 9.0
Почему: Прямая target, стек совпадает, финтех резонирует, юзер сам указал "вне РФ" и готов к релокации. Международность здесь — плюс, не минус.

ПРИМЕР 8 — резонирующая тема без функции (скор 5.5):
Профиль: BizDev/Partnerships Manager (4 года), expertise=[B2B продажи, партнёрства, юнит-экономика], target=[BD, Partnerships, Growth], interests_and_resonance=[AI, EdTech, аналитика]
Вакансия: "Data Analyst" в AI-стартапе (SQL, Python, ML-модели)
Скор: 5.5
Почему: Тема (AI) резонирует, но функция чужая — Data Analyst это другой день (запросы, модели, отчёты), а не переговоры и P&L. Юзер не сможет прийти без переквалификации. Резонирующая тема даёт +0.5, но не превращает в target.

ПРИМЕР 9 — target + резонирующая тема (скор 9.5):
Профиль: Product Marketing Manager, target=[Продуктовый маркетолог], interests_and_resonance=[Travel, международные рынки, e-commerce]
Вакансия: "Product Marketing Manager" в Travelpayouts (партнёрская сеть Aviasales, travel-индустрия)
Скор: 9.5
Почему: Прямая target-роль + индустрия travel резонирует с интересами + функция полностью совпадает. Отсутствие minor'ов делает это 9.5.

ПРИМЕР 10 — anti-role в явном виде (скор 3.5):
Профиль: BD/Growth Manager, anti_roles=[Продакт-менеджер, PM], expertise=[Партнёрства, юнит-экономика]
Вакансия: "Senior Product Manager - Growth" в EdTech-компании
Скор: 3.5
Почему: PM в явном виде. Даже "Growth PM" и "EdTech" (резонирующая тема) — это всё равно PM. Потолок 4.0, ставлю 3.5 потому что стрелка направлена в противоположную сторону от карьерных желаний.

ПРИМЕР 11 — правильный уровень + правильная функция (скор 9.5):
Профиль: IT-рекрутер 8+ лет, target=[Senior IT Recruiter, Head of Recruitment], expertise=[Full cycle IT recruitment, Executive Search]
Вакансия: "Head of Technical Recruiting" в Notion (глобальная продуктовая, remote-friendly)
Скор: 9.5
Почему: Прямое попадание в target Head of Recruitment, seniority соответствует (Senior/Lead уровень), Notion — продуктовая компания в резонирующей индустрии.

ПРИМЕР 12 — target по домену, но чужой контекст (скор 6.5):
Профиль: Проектировщик автодорог, target=[Руководитель проекта транспортной инфраструктуры]
Вакансия: "Ведущий инженер-проектировщик автодорог" (без управленческой функции)
Скор: 6.5
Почему: Домен идеально совпадает (автодороги), но это Individual Contributor позиция, а target-role управленческая. Anti-role частично задета ("проектировщик без управленческой функции"). Потолок 6.5 — понижение из-за отсутствия управленческого компонента.

ПРИМЕР 13 — правильно оценённый скор с гэпом (скор 7.5):
Профиль: Middle+ Growth Marketing, target=[Product Marketing Manager], location=РФ, remote_ok=True
Вакансия: "Product Marketing Manager" в российской IT-компании, гибрид Москва, зарплата подходит
Скор: 7.5
Почему: Прямая target-роль, локация ок, зарплата подходит. Минус — гибрид (юзер предпочитает remote), но это не deal-breaker. 7.5 отражает "хороший матч с одним неидеальным пунктом".

ПРИМЕР 14 — правильно оценённая узкая ниша (скор 9.0):
Профиль: Кандидат филологических наук, доцент 8 лет, target=[Автор, Автор публикаций, Редактор научно-популярных текстов]
Вакансия: "Автор курса" в Практикуме (образовательная платформа, ищут экспертов для создания курсов)
Скор: 9.0
Почему: Прямое попадание target-role + expertise (структурирование знаний, работа с текстом) совпадает + образование резонирует с академическим бэкграундом.

ПРИМЕР 15 — низкая релевантность несмотря на общие темы (скор 4.0):
Профиль: Backend разработчик Java (Sber, T-Bank, Alfa), target=[Senior Backend Developer], seniority=Senior
Вакансия: "Fullstack Developer (React + Node.js)" в стартапе
Скор: 4.5
Почему: Не target (Fullstack ≠ Backend Java), стек другой (Node.js ≠ Java), в expertise нет React/Node. Даже если тема "разработка" общая, функция другая. Потолок 5.0, ставлю 4.5.

ПРИМЕР 16 — чужая функция при богатом смежном профиле (скор 5.0):
Профиль: BD в подписочных продуктах (Ivi, Yandex Plus, T-Bank), expertise=[SQL (self-sufficient), юнит-экономика, аналитика], target=[BD, Partnerships, Growth]
Вакансия: "Data Scientist" в EdTech-стартапе (Python, ML, feature engineering)
Скор: 5.0
Почему: Тема (SQL, аналитика, EdTech) частично резонирует, но функция чужая — Data Scientist это Python+ML, а не переговоры и партнёрства. Юзер не сможет прийти без переквалификации. Потолок 5.0.

ПРИМЕР 17 — junior с потенциалом (скор 7.5):
Профиль: Junior веб-дизайнер, expertise=[Figma, Tilda], target=[Веб-дизайнер, UI/UX дизайнер]
Вакансия: "Senior UX/UI-дизайнер" в девелоперской компании, зарплата 298k, remote
Скор: 6.5
Почему: Target-роль по названию, но seniority mismatch — Senior позиция для Junior юзера это апгрейд на 2+ уровня. Потолок 6.5 (не 5.0, потому что дизайн — область где рост от Junior к Senior возможен быстрее, чем в разработке или менеджменте, и remote позволяет попробовать). Юзер может податься, но шансы прохождения ниже 30%.

ПРИМЕР 18 — резюме сначала звучит как no-match, но при чтении описания подходит (скор 7.5):
Профиль: Продюсер спецпроектов в EdTech (Netology), target=[Product Manager, Продюсер спецпроектов, Продакт полного цикла]
Вакансия: "Лид направления спецпроектов и ивентов" в Вышка Онлайн
Скор: 9.0
Почему: Прямое попадание target (Продюсер спецпроектов), EdTech-индустрия резонирует, "лидирование направления" совпадает с seniority Senior/Lead. Полное совпадение по функции + резонирующая индустрия.

────────────────────────────────────────

КРИТИЧНО: Твой ответ должен быть ТОЛЬКО валидным JSON. Ничего до, ничего после.
Никаких markdown-обёрток ```json```. Никаких объяснений после закрывающей скобки }.
Никаких вводных фраз типа "Вот разбор:". Просто JSON, начинается с { и заканчивается }.

Ответ:
{
  "score": число от 0 до 10 (можно с десятыми, например 7.5),
  "fit_reason": "ОДНО короткое предложение...",
  "red_flags": [...],
  "should_send": true/false
}

should_send=true если score >= 4.5.


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
        user_message = self._build_user_message(vacancy)
        profile_block = self._build_profile_block(profile_data)

        for attempt in range(2):
            response = await self.claude.chat(
                messages=[{"role": "user", "content": user_message}],
                system=MATCHER_SYSTEM_PROMPT,
                extra_system_blocks=[profile_block],
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

    def _build_profile_block(self, profile_data: dict) -> dict:
        """Profile as a separately-cached system block.

        Stays identical across all vacancy evaluations for one user in one cycle,
        so Anthropic cache hits on every call after the first.
        """
        profile_json = json.dumps(profile_data, ensure_ascii=False, indent=2)
        return {
            "type": "text",
            "text": f"ПРОФИЛЬ ЮЗЕРА:\n{profile_json}",
            "cache_control": {"type": "ephemeral"},
        }

    def _build_user_message(self, vacancy: Vacancy) -> str:
        description = (vacancy.description or "")[:MAX_DESCRIPTION_CHARS]
        return (
            f"ВАКАНСИЯ:\n"
            f"Название: {vacancy.title}\n"
            f"Компания: {vacancy.company or 'не указана'}\n"
            f"Локация: {vacancy.location or 'не указана'}\n"
            f"Зарплата: {vacancy.salary or 'не указана'}\n"
            f"Источник: {vacancy.source_type.value}\n"
            f"Описание:\n{description}\n\n"
            f"Оцени соответствие профилю выше. Верни JSON."
        )

    def _parse_response(self, text: str) -> MatchResult | None:
        if not text:
            return None

        # Извлекаем первый валидный JSON-объект из текста.
        # Haiku иногда оборачивает в ```json ... ``` фенсы и/или пишет
        # постскриптум вроде "Краткое объяснение:" после закрывающей }.
        json_str = self._extract_first_json_object(text)
        if json_str is None:
            return None

        try:
            data = json.loads(json_str)
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

    def _extract_first_json_object(self, text: str) -> str | None:
        """Находит первую { и её парную закрывающую }, возвращает подстроку.

        Учитывает вложенность и строки с фигурными скобками внутри значений.
        Возвращает None если валидный объект не найден.
        """
        # Начало
        start = text.find("{")
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape_next = False

        for i in range(start, len(text)):
            ch = text[i]

            if escape_next:
                escape_next = False
                continue

            if ch == "\\" and in_string:
                escape_next = True
                continue

            if ch == '"':
                in_string = not in_string
                continue

            if in_string:
                continue

            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]

        return None