"""Диагностика: у каких юзеров/индустрий не хватает источников.

Выводит:
1. Все активные юзеры с ключевыми полями профиля + количество доставок за 30 дней
2. Агрегацию по индустриям и ролям — где системная дыра
"""
import asyncio
import csv
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func
from src.db.session import async_session, engine
from src.db.models import Profile, User, VacancyMatch


CUTOFF_DAYS = 30


def _to_list(val) -> list[str]:
    """Приводим поле профиля к list[str] — оно может быть str, list или None."""
    if not val:
        return []
    if isinstance(val, list):
        return [str(x).strip() for x in val if x]
    if isinstance(val, str):
        return [val.strip()]
    if isinstance(val, dict):
        return [str(v).strip() for v in val.values() if v]
    return [str(val)]


async def main():
    cutoff = datetime.now(timezone.utc) - timedelta(days=CUTOFF_DAYS)

    async with async_session() as s:
        # 1. Активные юзеры с профилями
        users_result = await s.execute(
            select(User, Profile)
            .join(Profile, Profile.user_id == User.id)
            .where(User.is_active.is_(True))
            .where(User.profile_ready_for_search.is_(True))
        )
        rows = users_result.all()
        print(f"Активных юзеров с профилями: {len(rows)}\n")

        # 2. Для каждого — считаем доставки за 30 дней
        user_stats = []
        for user, profile in rows:
            deliveries_result = await s.execute(
                select(
                    func.count(VacancyMatch.id),
                    func.avg(VacancyMatch.match_score),
                    func.max(VacancyMatch.match_score),
                )
                .where(VacancyMatch.user_id == user.id)
                .where(VacancyMatch.sent_at >= cutoff)
            )
            count, avg_score, max_score = deliveries_result.one()

            pdata = profile.profile_data or {}
            user_stats.append({
                "telegram_id": user.telegram_id,
                "username": user.telegram_username or "—",
                "expertise": _to_list(pdata.get("expertise")),
                "target_roles": _to_list(pdata.get("target_roles")),
                "industries_interested": _to_list(pdata.get("industries_interested")),
                "languages": _to_list(pdata.get("languages")),
                "location_preferences": _to_list(pdata.get("location_preferences")),
                "deliveries_30d": count,
                "avg_score": round(avg_score, 2) if avg_score else None,
                "max_score": round(max_score, 2) if max_score else None,
            })

    # 3. Сортируем: сначала кому меньше всего доставляется
    user_stats.sort(key=lambda x: x["deliveries_30d"])

    print(f"=== ПОЮЗЕРНАЯ КАРТИНА (отсортировано по возрастанию доставок за {CUTOFF_DAYS} дн.) ===\n")
    for u in user_stats:
        print(f"@{u['username']} (tg={u['telegram_id']})")
        print(f"  Доставлено: {u['deliveries_30d']} | avg score: {u['avg_score']} | max: {u['max_score']}")
        print(f"  Экспертиза: {', '.join(u['expertise'])[:200]}")
        print(f"  Целевые роли: {', '.join(u['target_roles'])[:200]}")
        print(f"  Индустрии: {', '.join(u['industries_interested'])[:200]}")
        print(f"  Языки/локация: {', '.join(u['languages'])} | {', '.join(u['location_preferences'])}")
        print()

    # 4. Агрегация по индустриям — где системная дыра
    industry_stats: dict[str, list[int]] = defaultdict(list)
    for u in user_stats:
        for ind in u["industries_interested"]:
            industry_stats[ind.lower()].append(u["deliveries_30d"])

    print(f"\n=== ПО ИНДУСТРИЯМ (сколько юзеров интересуются + среднее доставок) ===\n")
    industry_sorted = sorted(
        industry_stats.items(),
        key=lambda x: sum(x[1]) / len(x[1]) if x[1] else 0,
    )
    for ind, deliveries in industry_sorted:
        avg = sum(deliveries) / len(deliveries) if deliveries else 0
        print(f"  [{len(deliveries)} юзер(ов), avg {avg:.1f} доставок] {ind}")

    # 5. Агрегация по target_roles
    role_stats: dict[str, list[int]] = defaultdict(list)
    for u in user_stats:
        for role in u["target_roles"]:
            role_stats[role.lower()].append(u["deliveries_30d"])

    print(f"\n=== ПО TARGET_ROLES (топ-20 с самым низким avg доставок) ===\n")
    role_sorted = sorted(
        role_stats.items(),
        key=lambda x: sum(x[1]) / len(x[1]) if x[1] else 0,
    )[:20]
    for role, deliveries in role_sorted:
        avg = sum(deliveries) / len(deliveries) if deliveries else 0
        print(f"  [{len(deliveries)} юзер(ов), avg {avg:.1f}] {role}")

    # 6. CSV на всякий случай
    with open("coverage_gaps.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "telegram_id", "username", "deliveries_30d", "avg_score", "max_score",
            "expertise", "target_roles", "industries", "languages", "locations",
        ])
        for u in user_stats:
            writer.writerow([
                u["telegram_id"], u["username"], u["deliveries_30d"],
                u["avg_score"], u["max_score"],
                " | ".join(u["expertise"]),
                " | ".join(u["target_roles"]),
                " | ".join(u["industries_interested"]),
                " | ".join(u["languages"]),
                " | ".join(u["location_preferences"]),
            ])
    print(f"\nCSV с полной картиной: coverage_gaps.csv")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
