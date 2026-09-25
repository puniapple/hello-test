"""Настройки канала вакансий. Всё, что можно крутить без правки логики."""
from __future__ import annotations

import os
import re

CHANNEL_ID = os.getenv("CHANNEL_ID", "@findfcknjobme")
MODEL = os.getenv("CHANNEL_MODEL", "claude-haiku-4-5-20251001")

POSTS_PER_RUN = 2          # сколько карточек за прогон (3 прогона в день)
MIN_SCORE = 6              # минимальная оценка Haiku для публикации
CANDIDATES_CAP = 80        # сколько кандидатов максимум отдаём Haiku на оценку
SCORE_BATCH = 20           # вакансий в одном запросе на оценку
MAX_AGE_DAYS = 3           # вакансии старше — не берём
MAX_PER_SOURCE = {"linkedin": 3}   # лимит на источник за прогон
MAX_PER_SOURCE_DEFAULT = 2
PAUSE_BETWEEN_POSTS = 3    # сек, антифлуд

# Теги карточки. Haiku выбирает только из этих списков; порядок в посте: формат → функция → страна → #вилка
TAGS_FORMAT = ["удалёнка", "гибрид", "офис", "релокация"]
TAGS_ROLE = ["продукт", "проджект", "growth", "маркетинг", "bizdev", "партнёрства",
             "аналитика", "продажи", "операционка", "стратегия", "pr"]
TAGS_GEO = ["россия", "казахстан", "узбекистан", "кыргызстан", "грузия", "армения",
            "сербия", "кипр", "оаэ", "турция"]
TAG_SALARY = "вилка"   # ставится кодом, если в карточке есть зарплата

# Компании, которые не постим никогда (сравнение по lower())
BLACKLIST_COMPANIES = {
    "micro1",
    "integrated language solutions",
    "careerspace",
}

INCLUDE = [
    r"product manager", r"product owner", r"продакт", r"менеджер продукт", r"продукт-менеджер",
    r"project manager", r"проджект", r"менеджер проект", r"руководитель проект",
    r"operations", r"операционн",
    r"growth", r"business development", r"bizdev", r"биздев", r"развити[юя] бизнеса",
    r"partnership", r"партн[её]рств", r"partner",
    r"marketing", r"маркетинг", r"маркетолог",
    r"analyst", r"аналитик",
    r"\bsales\b", r"продаж", r"account manager", r"account executive", r"аккаунт",
    r"strateg", r"стратег", r"финанс", r"\bfinance\b",
    r"\bpr\b", r"communications", r"коммуникац",
]
EXCLUDE = [
    r"design", r"дизайн", r"арт-директор", r"art director", r"creative",
    r"\bhr\b", r"hrbp", r"human resources", r"recruit", r"рекрут", r"talent acquisition",
    r"people operations", r"hris", r"payroll",
    r"developer", r"разработк", r"разработчик", r"engineer", r"инженер", r"программист",
    r"devops", r"\bqa\b", r"тестировщик", r"\btester\b", r"\b1[сc]\b",
    r"data scien", r"machine learning", r"\bml\b",
    r"копирайт", r"copywrit", r"\bsmm\b", r"\bсмм\b", r"контент", r"\bcontent\b",
    r"social media", r"социальн", r"community manager", r"модератор", r"обозреватель",
    r"support", r"поддержк", r"customer service",
    r"annotation", r"rlhf", r"ai trainer", r"ai-тренер",
    r"бухгалтер", r"accountant", r"\btax\b", r"counsel", r"юрист", r"\bgrc\b",
    r"executive assistant", r"ассистент",
    r"врач", r"медсестр", r"склад", r"курьер", r"водител", r"повар", r"кассир",
]

_INC = re.compile("|".join(INCLUDE), re.I)
_EXC = re.compile("|".join(EXCLUDE), re.I)


def in_niche(title: str, description: str | None) -> bool:
    title = title or ""
    if _EXC.search(title):
        return False
    return bool(_INC.search(f"{title} {(description or '')[:300]}"))


def is_blacklisted(company: str | None) -> bool:
    return (company or "").strip().lower() in BLACKLIST_COMPANIES


# ─── Доступность из СНГ (решение: удалёнка без привязки к стране + релокация) ───
_GEO_OK = re.compile(
    r"[а-яё]|kazakhstan|almaty|astana|tbilisi|georgia|armenia|yerevan|serbia|belgrade|"
    r"cyprus|limassol|nicosia|uzbekistan|tashkent|kyrgyz|bishkek|azerbaijan|baku|"
    r"montenegro|turkey|istanbul|dubai|united arab emirates|\buae\b|"
    r"worldwide|anywhere|global|emea|europe|\bcet\b", re.I)
_GEO_WIDE = re.compile(r"worldwide|anywhere|global|emea|europe", re.I)
_GEO_US = re.compile(
    r"united states|\busa?\b|u\.s\.|north america|\bamer\b|canada|new york|san francisco|"
    r"seattle|austin|boston|chicago|los angeles|atlanta|denver|pittsburgh|, [a-z]{2}\b", re.I)
_RELOC = re.compile(r"relocation (support|package|assistance|bonus)|relocat\w* (is )?(provided|offered|available)|"
                    r"visa sponsorship|visa support|релокац", re.I)
_NO_RELOC = re.compile(r"(not|unable to|cannot|can't|no|without) (be able to )?(offer |provide )?(visa )?(sponsor|relocat)", re.I)


def geo_ok(location: str | None, description: str | None) -> bool:
    """Можно ли реально получить вакансию из СНГ. Неясные случаи пропускаем — решит Haiku."""
    loc, desc = location or "", (description or "")[:4000]
    reloc = bool(_RELOC.search(desc)) and not _NO_RELOC.search(desc)
    if _GEO_US.search(loc) and not _GEO_WIDE.search(loc):
        return reloc
    if _GEO_OK.search(loc):
        return True
    if reloc:
        return True
    if not loc.strip() or re.search(r"remote|удал[её]н", loc, re.I):
        return True
    return False  # офис вне СНГ без релокации
