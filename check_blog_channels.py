"""Проверка блогов: в каких каналах бывают посты о найме.

Берёт список из blog_candidates.txt (по одному handle в строке) + 25 блогов руководителей.
Для каждого: публичный ли канал, подписчики, период последних ~20 постов,
сколько постов о найме (строгие маркеры) и сколько из них по нише канала.

Результат пишется в blog_check.csv построчно. Если прогон оборвался — просто
запусти снова: уже проверенные каналы пропускаются.

Запуск из корня репо:
    python check_blog_channels.py
Займёт ~15–25 минут на ~2900 каналов.
"""
import asyncio
import csv
import os
import re
from datetime import datetime

import httpx
from bs4 import BeautifulSoup

try:
    from src.channel.config import in_niche
except Exception:  # noqa: BLE001
    in_niche = None

LEADERS = [
    "tired_glebmikheev", "maltsevprosto", "ngitsmylife", "busy_bee_vishnevskaya", "blog_toxa",
    "peachyleader", "teamleading", "milaivy", "bykovdigital", "ulshinblog", "leysan_answers",
    "qagarage", "Khorenyan", "makeev_sexiIT", "boombah_in_da_house", "meet_egorov",
    "teolog_marketolog_channel", "ai4bus", "invest_to_head", "mishanga_channel", "oo_ilin",
    "dmitriy_movchan_pro_it", "another_mvp", "FromRon", "threetaskproblem",
]
OUT = "blog_check.csv"
FIELDS = ["handle", "status", "subs", "posts", "period", "hiring", "hiring_niche", "samples"]
CONCURRENCY = 5

HIRING = re.compile(
    r"ищ(у|ем)\s+(\S+\s+){0,3}(в\s+)?(свою |нашу |мою )?команд|"
    r"в\s+(мою|нашу|свою)\s+команду\s+(ищ|нуж)|"
    r"открыт[аы]?\s+(ваканси|позици)|ваканси[яю]\s*[:—-]|"
    r"we('re| are)\s+hiring|\bhiring\b|нанима(ю|ем)|"
    r"присылайте\s+резюме|резюме\s+(в\s+лс|в\s+личку|сюда|мне)|"
    r"откликнуться|пишите\s+(мне\s+)?в\s+(лс|личку)\s+.*резюме",
    re.I,
)
UA = {"User-Agent": "Mozilla/5.0"}


def load_handles() -> list[str]:
    handles = list(LEADERS)
    if os.path.exists("blog_candidates.txt"):
        handles += [h.strip() for h in open("blog_candidates.txt", encoding="utf-8") if h.strip()]
    seen, out = set(), []
    for h in handles:
        if h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


def done_handles() -> set[str]:
    if not os.path.exists(OUT):
        return set()
    with open(OUT, encoding="utf-8") as f:
        return {row["handle"].lower() for row in csv.DictReader(f)}


async def check(client, handle) -> dict:
    row = {k: "" for k in FIELDS}
    row["handle"] = handle
    for attempt in range(4):
        try:
            r = await client.get(f"https://t.me/s/{handle}", headers=UA, follow_redirects=False)
        except httpx.HTTPError as e:
            row["status"] = f"ERR {type(e).__name__}"
            return row
        if r.status_code == 429:
            await asyncio.sleep(10 * (attempt + 1))
            continue
        break
    if r.status_code != 200:
        row["status"] = "не канал"  # личный аккаунт, группа, приватный или не существует
        return row
    soup = BeautifulSoup(r.text, "html.parser")
    if not soup.select_one(".tgme_channel_info"):
        row["status"] = "не канал"
        return row
    for c in soup.select(".tgme_channel_info_counter"):
        if "subscriber" in c.get_text():
            row["subs"] = c.select_one(".counter_value").get_text(strip=True)
    posts, dates, hits, niche_hits = soup.select(".tgme_widget_message_wrap"), [], [], 0
    for p in posts:
        t = p.select_one(".tgme_widget_message_date time[datetime]")
        if t:
            try:
                dates.append(datetime.fromisoformat(t["datetime"]))
            except ValueError:
                pass
        node = p.select_one(".tgme_widget_message_text")
        text = node.get_text("\n", strip=True) if node else ""
        if text and HIRING.search(text):
            first = next((ln for ln in text.split("\n") if len(ln.strip()) > 5), "")[:80]
            hits.append(first)
            if in_niche and in_niche(text[:200], text[:600]):
                niche_hits += 1
    row.update(status="ok", posts=len(posts),
               period=f"{min(dates).date()}…{max(dates).date()}" if dates else "",
               hiring=len(hits), hiring_niche=niche_hits if in_niche else "", samples=" | ".join(hits[:3]))
    return row


async def main():
    todo = [h for h in load_handles() if h.lower() not in done_handles()]
    print(f"Осталось проверить: {len(todo)}", flush=True)
    new_file = not os.path.exists(OUT)
    sem = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()
    counter = {"n": 0}

    with open(OUT, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new_file:
            w.writeheader()

        async def one(client, h):
            async with sem:
                row = await check(client, h)
                await asyncio.sleep(0.5)
            async with lock:
                w.writerow(row)
                f.flush()
                counter["n"] += 1
                if counter["n"] % 100 == 0:
                    print(f"  …{counter['n']}/{len(todo)}", flush=True)

        async with httpx.AsyncClient(timeout=15) as client:
            await asyncio.gather(*(one(client, h) for h in todo))

    with open(OUT, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    ok = [r for r in rows if r["status"] == "ok"]
    hiring = sorted((r for r in ok if int(r["hiring"] or 0) > 0),
                    key=lambda r: (int(r["hiring_niche"] or 0), int(r["hiring"])), reverse=True)
    print(f"\n═══ проверено {len(rows)}: каналов {len(ok)}, не каналов {len(rows) - len(ok)}, "
          f"с постами о найме {len(hiring)}")
    print(f"{'канал':<30} {'subs':<7} {'найм':>4} {'ниша':>4}  период")
    for r in hiring:
        print(f"{r['handle']:<30} {r['subs']:<7} {r['hiring']:>4} {r['hiring_niche']:>4}  {r['period']}")
    print(f"\nПолная таблица — {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
