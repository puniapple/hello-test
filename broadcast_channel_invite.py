"""Broadcast приглашение в @puniapple_findjob — спецам из бизнеса.

Собирает юзеров из БД по бакетам: product, project/ops, growth/bd, marketing,
sales, content/smm, analytics, PR, стратегия/финансы.

Исключает: HR, design, разработку, QA, DevOps, ассистентов, event, L&D,
антифрод, медицину, кухню, производство, ручной труд, перевод.

DRY_RUN режим — сначала показывает preview, потом ждёт подтверждения.

Запуск: python3 broadcast_channel_invite.py
Лежит в .gitignore, коммитить не надо.
"""
from __future__ import annotations

import asyncio
from collections import Counter

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from src.config import settings
from src.db.models import Profile, User
from src.db.session import async_session, engine

BOT_TOKEN = settings.telegram_bot_token
if not BOT_TOKEN:
    raise RuntimeError("telegram_bot_token не задан в settings")

DELAY_BETWEEN_SENDS_SEC = 0.5


BROADCAST_TEXT = (
    "Привет! Я запустила канал с вакансиями для спецов из бизнеса — "
    "продакты, проджекты, growth, маркетинг, продажи, аналитика.\n\n"
    "Удалёнка и релокейт в сильные компании. Актуальные вакансии каждый день.\n\n"
    "@puniapple_findjob — заходи, если ещё в поиске."
)


# ─── Классификатор доменов ───
INCLUDE_KEYWORDS = {
    "product": [
        "product manager", "продакт", "product owner", "product marketing",
        "head of product", "product analyst",
    ],
    "project_ops": [
        "project manager", "проджект", "проектный менеджер", "delivery manag",
        "programme manag", "operations coordin", "operations manag",
        "chief of staff", "coo", "director of operations", "operations",
    ],
    "growth_bd": [
        "growth manager", "head of growth", "bizdev", "biz dev",
        "business development", "partnership", "new business", "new ventures",
        "партнёрств", "партнерств", "development manager", "growth",
    ],
    "marketing": [
        "cmo", "head of marketing", "marketing director", "директор по маркетинг",
        "team lead marketing", "marketing manager", "director of growth",
        "бренд-менеджер", "продуктовый маркетолог", "маркетолог", "маркетинг",
    ],
    "sales": [
        "sales", "продаж", "коммерческий директор", "директор по продажам",
        "head of sales", "key account", "account manager", "customer success",
        "kam",
    ],
    "content_smm": [
        "copywriter", "копирайтер", "контент-менеджер", "content manager",
        "content writer", "content editor", "редактор", "smm",
        "комьюнити-менеджер", "автор", "журналист",
    ],
    "analytics": [
        "data scientist", "data analyst", "аналитик данных",
        "head of analytics", "analytics manager", "ml eng",
        "маркетинговый аналитик", "аналитик аудитории",
    ],
    "pr": [
        "pr-менеджер", "pr менеджер", "коммуникац", "внутренних коммуникац",
        "антикризисный pr",
    ],
    "strategy_finance": [
        "стратегический аналитик", "инвестицион", "m&a", "финансов",
        "стратег",
    ],
}

# Исключения — если совпало, юзер отсекается, даже если попал в include
EXCLUDE_KEYWORDS = [
    # HR / рекрутмент
    "recruiter", "рекрутер", "рекрутмент", "hr manager", "hr generalist",
    "hrbp", "hr business partner", "head of hr", "hrd", "people partner",
    "директор по персоналу",
    # Дизайн
    "designer", "ui/ux", "ui / ux", "ui дизайнер", "ux дизайнер",
    "веб-дизайнер", "art director", "creative director", "art lead",
    "head of art", "3d-визуал", "3d artist", "3d визуал",
    # Разработка / инженерия
    "developer", "engineer", "разработчик", "backend", "frontend",
    "android", "ios", "fullstack", "full-stack",
    # QA
    "qa engineer", "qa automation", "sdet", "qa team lead", "тестировщик",
    # DevOps
    "devops", "sre", "sysadmin",
    # Отдельные ниши
    "ассистент", "assistant", "помощник руководителя",
    "продюсер", "event-менеджер",
    "бизнес-тренер", "методолог", "l&d",
    "anti-fraud", "антифрод", "head of risk", "риск-менеджер",
    # E-commerce маркетплейсы и customer service
    "менеджер маркетплейс", "маркетплейс",
    "чат-оператор", "онлайн-консультант", "оператор чата",
    "контент-модератор", "модератор",
    # Медицина/производство/кухня/спец
    "врач", "гастроэнтеролог", "эндоскопист",
    "шеф-повар", "су-шеф",
    "надомный", "оператор печ", "печат", "кладовщик", "приёмщик", "сборщик",
    "плк", "автоматизация систем", "кипиа",
    "градостроит", "проектирование дорог", "проектирование автодорог",
    "переводчик", "лингвист", "филолог",
    # Системный/бизнес-аналитик — не наша ниша
    "системный аналитик", "system analy", "бизнес-аналитик", "business analy",
]


def matches_target(profile_data: dict) -> tuple[bool, str]:
    """Возвращает (подходит ли, какой бакет)."""
    target = (profile_data.get("target_roles") or "")
    expertise = (profile_data.get("expertise") or "")
    if isinstance(target, list):
        target = ", ".join(str(x) for x in target)
    if isinstance(expertise, list):
        expertise = ", ".join(str(x) for x in expertise)

    text = (target + " " + expertise).lower()

    # Сначала исключения
    for excl in EXCLUDE_KEYWORDS:
        if excl in text:
            return False, f"excluded:{excl}"

    # Потом include
    for bucket, keywords in INCLUDE_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return True, bucket

    return False, "no_match"


async def collect_targets() -> list[tuple[User, str, str]]:
    """Собирает всех active юзеров, проходящих фильтр.
    Возвращает [(user, bucket, target_preview), ...]."""
    async with async_session() as s:
        result = await s.execute(
            select(User, Profile)
            .join(Profile, Profile.user_id == User.id)
            .where(User.is_active.is_(True))
        )
        rows = result.all()

    targets = []
    for user, profile in rows:
        pd = profile.profile_data or {}
        if not pd:
            continue
        ok, bucket = matches_target(pd)
        if ok:
            target_preview = str(pd.get("target_roles") or "")[:80]
            targets.append((user, bucket, target_preview))
    return targets


async def send_broadcast(bot: Bot, targets: list[tuple[User, str, str]]) -> dict:
    stats = {"total": len(targets), "sent": 0, "blocked": 0, "errors": 0}
    if not targets:
        print("Никого нет для broadcast.")
        return stats

    print(f"\n=== Отправка {len(targets)} юзерам ===")

    for i, (user, bucket, _) in enumerate(targets, 1):
        username = f"@{user.telegram_username}" if user.telegram_username else "—"
        try:
            await bot.send_message(
                chat_id=user.telegram_id,
                text=BROADCAST_TEXT,
                disable_web_page_preview=False,
            )
            stats["sent"] += 1
            print(f"  [{i}/{len(targets)}] ✅ {username} ({bucket})")
        except TelegramAPIError as e:
            err_str = str(e).lower()
            if "blocked" in err_str or "bot was blocked" in err_str or "user is deactivated" in err_str:
                stats["blocked"] += 1
                print(f"  [{i}/{len(targets)}] 🚫 {username}: заблокировали бота")
            else:
                stats["errors"] += 1
                print(f"  [{i}/{len(targets)}] ❌ {username}: {e}")
        except Exception as e:
            stats["errors"] += 1
            print(f"  [{i}/{len(targets)}] ❌ {username}: {e}")

        await asyncio.sleep(DELAY_BETWEEN_SENDS_SEC)

    return stats


async def main():
    targets = await collect_targets()

    print("=" * 70)
    print("BROADCAST — приглашение в @puniapple_findjob")
    print("=" * 70)
    print(f"Целевых юзеров: {len(targets)}")
    print()
    print("Текст:")
    print("-" * 70)
    print(BROADCAST_TEXT)
    print("-" * 70)
    print()

    bucket_counts = Counter(t[1] for t in targets)
    print("Распределение по бакетам:")
    for b, c in bucket_counts.most_common():
        print(f"  {c:3d}  {b}")
    print()

    print("Preview выборки (username | bucket | target_roles):")
    for user, bucket, preview in targets:
        username = f"@{user.telegram_username}" if user.telegram_username else f"tg:{user.telegram_id}"
        print(f"  {username:25s} | {bucket:20s} | {preview}")
    print()

    if not targets:
        print("Пустая выборка. Завершаю.")
        await engine.dispose()
        return

    confirmation = input(
        f"Отправить сообщение {len(targets)} юзерам? (yes/no): "
    ).strip().lower()
    if confirmation not in ("yes", "y", "да", "д"):
        print("Отменено.")
        await engine.dispose()
        return

    bot = Bot(token=BOT_TOKEN)
    try:
        stats = await send_broadcast(bot, targets)
    finally:
        await bot.session.close()
        await engine.dispose()

    print()
    print("=" * 70)
    print("ИТОГО")
    print("=" * 70)
    print(f"  Отправлено:      {stats['sent']}")
    print(f"  Заблокировали:   {stats['blocked']}")
    print(f"  Ошибки:          {stats['errors']}")


if __name__ == "__main__":
    asyncio.run(main())
