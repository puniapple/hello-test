"""Проверка кандидатов в TG-источники для канала.

Для каждого handle: публичный ли канал (читается ли t.me/s/), подписчики,
дата последнего поста, постов за 7 дней, сколько вакансий прошло фильтр ниши.

Запуск из корня репо (рядом должен лежать check_channel_flow.py):
    python check_new_channels.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
from bs4 import BeautifulSoup

from check_channel_flow import in_niche
from src.sources.telegram_channel import TelegramChannelSource

CANDIDATES = [
    # маркетинг / PR / digital
    "digital_jobster", "rabota_go", "jobforpr", "runello_rus_digitalmarketing",
    "marketing_rabota_jobs", "workmarketingg", "hunty_jobs", "mediajobs_ru", "tj_collega",
    # продукт / проекты / аналитика / продажи / топы
    "forallsales", "jobfortm", "forchiefs", "runello_jobs_product", "runello_jobs_sales",
    "runello_jobs_analyst", "jabroad", "c_level_top", "vacancyCRC", "bigtechjobs",
    # работодатели
    "ya_jobs", "careersber", "mtsbankcareer", "t_crew",
    # удалёнка / релокация
    "young_relocate", "evacuatejobs", "normremote", "work_finde", "digital_rabota",
    "youritjob", "Remoteit", "hiddengurus", "jobs_inarmenia", "networkio_io",
    "noborders_forwomen", "agilefluent", "jobsincyprus",
    # из папки work
    "jobcatcher", "theyseeku_it",
]

UA = {"User-Agent": "Mozilla/5.0"}
WEEK_AGO = datetime.now(timezone.utc) - timedelta(days=7)


async def page_info(client, handle):
    try:
        r = await client.get(f"https://t.me/s/{handle}", headers=UA, follow_redirects=False)
    except httpx.HTTPError as e:
        return {"status": f"ERR {type(e).__name__}"}
    if r.status_code != 200:
        return {"status": f"HTTP {r.status_code} (группа/приватный/нет)"}
    soup = BeautifulSoup(r.text, "html.parser")
    if not soup.select_one(".tgme_channel_info"):
        return {"status": "не канал (группа или пусто)"}
    subs = "?"
    for c in soup.select(".tgme_channel_info_counter"):
        if "subscriber" in c.get_text():
            subs = c.select_one(".counter_value").get_text(strip=True)
    dates = []
    for t in soup.select(".tgme_widget_message_date time[datetime]"):
        try:
            dates.append(datetime.fromisoformat(t["datetime"]))
        except ValueError:
            pass
    return {
        "status": "ok",
        "subs": subs,
        "last": max(dates).date().isoformat() if dates else "-",
        "week": sum(d >= WEEK_AGO for d in dates),
    }


async def main():
    tg = TelegramChannelSource()
    async with httpx.AsyncClient(timeout=15) as client:
        for h in CANDIDATES:
            info = await page_info(client, h)
            niche = "-"
            if info["status"] == "ok":
                try:
                    vacs = await tg.fetch(SimpleNamespace(identifier=h, is_active=True, user_id=None))
                    niche = f"{sum(in_niche(v) for v in vacs)}/{len(vacs)}"
                except Exception as e:  # noqa: BLE001
                    niche = f"ERR {type(e).__name__}"
            print(f"{h:<30} {info['status']:<32} subs={info.get('subs','-'):<7} "
                  f"last={info.get('last','-'):<11} 7д={info.get('week','-'):<3} ниша={niche}")
            await asyncio.sleep(1.5)


if __name__ == "__main__":
    asyncio.run(main())
