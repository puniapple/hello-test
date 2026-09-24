"""Проверка потока вакансий для канала.

Дёргает все источники бота напрямую: без БД, без Haiku, без Telegram-токена.
Считает по каждому источнику: жив ли, сколько вернул, сколько проходит нишу канала,
сколько из них свежие (за 7 дней), сколько remote/гибрид.

Запуск из корня репо:
    python check_channel_flow.py

Результат: таблица в консоли + channel_flow_sample.csv (все вакансии, прошедшие фильтр).
"""
from __future__ import annotations

import asyncio
import csv
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

from src.sources.career_sites import CareerSiteSource, get_career_site_ids
from src.sources.channels_config import TELEGRAM_CHANNELS
from src.sources.telegram_channel import TelegramChannelSource

try:
    from src.sources.hh_ru import HHSource
except Exception:  # noqa: BLE001
    HHSource = None

# ─── Фильтр ниши канала (из ТЗ) ───
INCLUDE = [
    r"product manager", r"product owner", r"продакт", r"менеджер продукт", r"продукт-менеджер",
    r"project manager", r"проджект", r"менеджер проект", r"руководитель проект",
    r"operations", r"операционн",
    r"growth", r"business development", r"bizdev", r"биздев", r"развити[юя] бизнеса",
    r"partnership", r"партн[её]рств",
    r"marketing", r"маркетинг", r"маркетолог",
    r"analyst", r"аналитик",
    r"\bsales\b", r"продаж", r"account manager", r"account executive", r"аккаунт",
    r"strateg", r"стратег", r"финанс", r"\bfinance\b",
    r"\bpr\b", r"communications", r"коммуникац",
]
EXCLUDE = [
    r"design", r"дизайн",
    r"\bhr\b", r"hrbp", r"human resources", r"recruit", r"рекрут", r"talent acquisition",
    r"developer", r"разработчик", r"engineer", r"инженер", r"программист", r"devops",
    r"\bqa\b", r"тестировщик", r"\btester\b",
    r"data scien", r"machine learning", r"\bml\b",
    r"копирайт", r"copywrit", r"\bsmm\b", r"контент", r"\bcontent\b",
    r"support", r"поддержк",
    r"врач", r"медсестр", r"склад", r"курьер", r"водител", r"повар", r"кассир",
]
REMOTE = [r"remote", r"удал[её]н", r"гибрид", r"hybrid", r"anywhere", r"relocat", r"релокац"]

INC = re.compile("|".join(INCLUDE), re.I)
EXC = re.compile("|".join(EXCLUDE), re.I)
REM = re.compile("|".join(REMOTE), re.I)

SOURCE_TIMEOUT = 90
CONCURRENCY = 8
NOW = datetime.now(timezone.utc)
WEEK_AGO = NOW - timedelta(days=7)


def in_niche(v) -> bool:
    title = v.title or ""
    head = f"{title} {(v.description or '')[:300]}"
    if EXC.search(title):
        return False
    return bool(INC.search(head))


def is_remote(v) -> bool:
    return bool(REM.search(f"{v.title} {v.location or ''} {(v.description or '')[:500]}"))


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    s = str(value).strip()
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(s)
    except (TypeError, ValueError):
        return None


async def run_one(sem, label, fetcher, identifier):
    async with sem:
        src = SimpleNamespace(identifier=identifier, is_active=True, user_id=None)
        try:
            vacs = await asyncio.wait_for(fetcher.fetch(src), timeout=SOURCE_TIMEOUT)
            return label, vacs or [], None
        except Exception as e:  # noqa: BLE001
            return label, [], f"{type(e).__name__}: {str(e)[:80]}"


async def main():
    sem = asyncio.Semaphore(CONCURRENCY)
    cs, tg = CareerSiteSource(), TelegramChannelSource()

    tasks = [run_one(sem, f"site:{sid}", cs, sid) for sid in get_career_site_ids()]
    tasks += [run_one(sem, f"tg:{ch}", tg, ch) for ch in dict.fromkeys(TELEGRAM_CHANNELS)]
    if HHSource is not None:
        tasks.append(run_one(sem, "hh:product manager (эксперимент)", HHSource(), "product manager"))

    print(f"Проверяю {len(tasks)} источников, это займёт пару минут...\n")
    results = await asyncio.gather(*tasks)

    rows, seen, sample = [], set(), []
    totals = defaultdict(int)
    for label, vacs, err in results:
        niche = [v for v in vacs if in_niche(v)]
        fresh = [v for v in niche if (d := parse_dt(v.published_at)) and d >= WEEK_AGO]
        remote = [v for v in fresh if is_remote(v)]
        rows.append((label, "ERR" if err else ("0" if not vacs else "ok"), len(vacs), len(niche), len(fresh), len(remote), err))

        for v in vacs:
            fp = v.content_fingerprint
            if fp in seen:
                continue
            seen.add(fp)
            totals["all"] += 1
            if in_niche(v):
                totals["niche"] += 1
                d = parse_dt(v.published_at)
                f = bool(d and d >= WEEK_AGO)
                r = is_remote(v)
                totals["niche_7d"] += f
                totals["niche_7d_remote"] += f and r
                totals["niche_nodate"] += d is None
                sample.append([label, v.title, v.company or "", v.location or "", v.salary or "",
                               d.date().isoformat() if d else "", "да" if r else "", v.url])

    rows.sort(key=lambda r: (-r[4], -r[3], r[0]))
    print(f"{'источник':<45} {'статус':<6} {'всего':>6} {'ниша':>5} {'7д':>4} {'7д+remote':>9}")
    print("-" * 80)
    for label, st, total, n, f, r, err in rows:
        print(f"{label[:45]:<45} {st:<6} {total:>6} {n:>5} {f:>4} {r:>9}")
        if err:
            print(f"    └ {err}")

    dead = [r[0] for r in rows if r[1] != "ok"]
    print("\n═══ ИТОГО (уникальные вакансии, без дублей между источниками) ═══")
    print(f"Источников: {len(rows)}, живых: {len(rows) - len(dead)}, мёртвых/пустых: {len(dead)}")
    print(f"Всего вакансий:            {totals['all']}")
    print(f"Прошли нишу канала:        {totals['niche']}")
    print(f"  из них за 7 дней:        {totals['niche_7d']}")
    print(f"  за 7 дней + remote/гибрид: {totals['niche_7d_remote']}")
    print(f"  без даты публикации:     {totals['niche_nodate']}")

    with open("channel_flow_sample.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["source", "title", "company", "location", "salary", "published", "remote", "url"])
        w.writerows(sample)
    print("\nВсе вакансии ниши сохранены в channel_flow_sample.csv")


if __name__ == "__main__":
    asyncio.run(main())
