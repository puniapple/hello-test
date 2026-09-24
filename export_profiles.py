"""Экспорт профилей юзеров в CSV для аналитики.

Тянет profile_data JSON из таблицы profiles, разворачивает основные поля
в плоскую таблицу. Ничего не нормализует — данные as-is, чтобы Claude
дальше проанализировал их вне БД.

Запуск: python3 export_profiles.py
Результат: profiles_export.csv в корне репо.
"""
from __future__ import annotations

import asyncio
import csv
import json

from sqlalchemy import select
from src.db.session import async_session, engine
from src.db.models import Profile, User, VacancyMatch, ScoreBreakdown


def _as_str(v) -> str:
    """Список → 'a, b, c'. Dict → JSON. Прочее → str."""
    if v is None:
        return ""
    if isinstance(v, list):
        return ", ".join(_as_str(x) for x in v if x)
    if isinstance(v, dict):
        return json.dumps(v, ensure_ascii=False)
    return str(v).strip()


async def main():
    async with async_session() as s:
        # Все юзеры с профилями (не пустыми)
        rows = (await s.execute(
            select(User, Profile)
            .join(Profile, Profile.user_id == User.id)
        )).all()

        result_rows = []
        for user, profile in rows:
            pd = profile.profile_data or {}
            if not pd:
                # Пустой профиль — пропускаем, аналитике бесполезен
                continue

            # Количество доставок и разборов
            deliveries = (await s.execute(
                select(VacancyMatch)
                .where(VacancyMatch.user_id == user.id)
            )).scalars().all()
            breakdowns = (await s.execute(
                select(ScoreBreakdown)
                .where(ScoreBreakdown.user_id == user.id)
            )).scalars().all()

            result_rows.append({
                "telegram_id": user.telegram_id,
                "telegram_username": user.telegram_username or "",
                "plan": user.plan or "",
                "subscription_status": user.subscription_status or "",
                "is_active": user.is_active,
                "profile_ready": user.profile_ready_for_search,
                "registered_at": user.created_at.strftime("%Y-%m-%d") if user.created_at else "",
                "deliveries_total": len(deliveries),
                "breakdowns_total": len(breakdowns),
                # ── Поля профиля ──
                "expertise": _as_str(pd.get("expertise")),
                "target_roles": _as_str(pd.get("target_roles")),
                "anti_roles": _as_str(pd.get("anti_roles")),
                "industries_interested": _as_str(pd.get("industries_interested")),
                "industries_experience": _as_str(pd.get("industries_experience")),
                "seniority": _as_str(pd.get("seniority")),
                "years_of_experience": _as_str(pd.get("years_of_experience")),
                "work_format": _as_str(pd.get("work_format")),
                "location": _as_str(pd.get("location")),
                "location_preferences": _as_str(pd.get("location_preferences")),
                "min_monthly": _as_str(pd.get("min_monthly")),
                "salary_expectations": _as_str(pd.get("salary_expectations")),
                "languages": _as_str(pd.get("languages")),
                "ideal_work_description": _as_str(pd.get("ideal_work_description")),
                "additional_context": _as_str(pd.get("additional_context")),
            })

    if not result_rows:
        print("Нет профилей для экспорта.")
        await engine.dispose()
        return

    fieldnames = list(result_rows[0].keys())
    with open("profiles_export.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(result_rows)

    print(f"Экспортировано {len(result_rows)} профилей → profiles_export.csv")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
