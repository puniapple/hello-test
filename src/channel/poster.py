"""Модуль постинга вакансий в канал.

run_channel_cycle(bot, dry_run=True) — сухой прогон: печатает карточки, ничего не шлёт и не пишет в БД.
run_channel_cycle(bot, dry_run=False) — боевой: публикует в CHANNEL_ID и пишет в posted_to_channel.
"""
from __future__ import annotations

import asyncio
import dataclasses
import html
import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from types import SimpleNamespace

import httpx
from anthropic import AsyncAnthropic
from bs4 import BeautifulSoup
from sqlalchemy import text

from src.channel import config as cfg
from src.config import settings
from src.db.session import async_session
from src.sources.base import Vacancy
from src.sources.career_sites import CareerSiteSource, get_career_site_ids
from src.sources.channels_config import TELEGRAM_CHANNELS
from src.sources.telegram_channel import TelegramChannelSource

logger = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0"}

SCORE_SYSTEM = """Ты редактор Telegram-канала с вакансиями для digital-специалистов из СНГ.
Ниша: продакты, проджекты, operations, маркетинг (performance, product, brand), growth, BD и партнёрства, аналитика (продуктовая, бизнес, системная, маркетинговая), продажи и аккаунтинг, стратегия и финансы, PR и коммуникации.
Аудитория: мидлы и сеньоры из России, Казахстана, Кыргызстана, Узбекистана, Грузии, Армении, Сербии и других стран СНГ и релокации.

Для каждой вакансии реши, постить ли её, и поставь оценку 0-10.
Сначала проверь доступность из СНГ: офис или гибрид в США, Канаде, Западной Европе и других странах вне СНГ без явной релокации — это post=false, даже если компания сильная.

post=false, если:
- это не одна конкретная вакансия: дайджест, подборка, статья, реклама курса или сервиса, пост «ищу работу»;
- роль вне ниши: разработка, QA, дизайн, HR, data science/ML, контент/SMM/копирайт, поддержка клиентов, бухгалтерия, юристы, узкие ниши;
- подработка по разметке данных, обучению ИИ, тестированию;
- вакансия явно недоступна из СНГ: работа только из США или конкретной страны вне СНГ без релокации, требуется гражданство или разрешение на работу.

Оценка при post=true:
8-10 — сильная компания или стартап с понятным продуктом, ясная роль, уровень мидл и выше, есть вилка или удалёнка/релокация;
5-7 — нормальная вакансия без ярких плюсов;
0-4 — слабая: джун или стажёр, мутное описание, продажи «на процентах», агентство без названия компании.

Ответ — ТОЛЬКО JSON-массив, без пояснений до или после и без ```:
[{"i": 0, "post": true, "score": 7, "reason": "до 10 слов"}]
Ровно по одному объекту на каждую вакансию из запроса."""

CARD_SYSTEM = """Ты пишешь карточку вакансии для Telegram-канала. Язык — русский, даже если вакансия на английском.
Тон нейтральный, без восклицаний и рекламных слов («крутая команда», «уникальная возможность»).
Ничего не выдумывай: если данных нет — null.

Верни ТОЛЬКО JSON-объект, без пояснений и без ```:
{"post": true,
 "role": "название роли как в вакансии (можно на английском)",
 "sphere": "сфера компании 1-3 словами (финтех, e-commerce, edtech...) или null",
 "format": "Удалёнка / Гибрид, Алматы / Офис, Москва / Релокация в ... или null",
 "company": "название компании или null",
 "salary": "вилка как в тексте или null",
 "summary": "2-3 коротких предложения: чем занимается роль и ключевое требование. До 350 символов.",
 "apply": номер ссылки из списка «Ссылки из поста» или null,
 "tags": ["теги без #, только из разрешённых списков ниже"]}

post=false, если текст оказался не вакансией (дайджест, реклама) или вакансия недоступна из СНГ.

Про apply (только если в запросе есть «Ссылки из поста»):
выбери ссылку, по которой кандидат откликается у работодателя: страница вакансии на сайте компании или в её ATS, форма отклика, hh.ru, контакт рекрутера или HR в Telegram.
НЕ подходят: ссылки на размещение вакансий и рекламу в канале, на другие каналы и чаты, на курсы, сервисы карьерной поддержки, соцсети компании, реестр РКН.
Если подходящей ссылки нет — apply=null.

Про tags — бери ТОЛЬКО из этих списков, ничего своего:
- формат (1-2): """ + ", ".join(cfg.TAGS_FORMAT) + """
- функция (1-2): """ + ", ".join(cfg.TAGS_ROLE) + """
- страна (0-1, только для офиса, гибрида или релокации, если страна есть в списке): """ + ", ".join(cfg.TAGS_GEO)


# ─── утилиты ───

def _client() -> AsyncAnthropic:
    return AsyncAnthropic(api_key=settings.anthropic_api_key)


async def _haiku(system: str, user: str, max_tokens: int) -> str:
    resp = await _client().messages.create(
        model=cfg.MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _extract_json(raw: str, open_ch: str):
    close_ch = "]" if open_ch == "[" else "}"
    start, end = raw.find(open_ch), raw.rfind(close_ch)
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except ValueError:
        return None


def _parse_dt(value):
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


def _is_tg(v: Vacancy) -> bool:
    return str(getattr(v.source_type, "value", v.source_type)) == "telegram_channel"


def _source(v: Vacancy) -> str:
    if _is_tg(v):
        return f"tg:{(v.raw or {}).get('channel', '?')}"
    return (v.raw or {}).get("site") or v.external_id.split(":", 1)[0]


def _source_limit(src: str) -> int:
    return cfg.MAX_PER_SOURCE.get(src, cfg.MAX_PER_SOURCE_DEFAULT)


def _strip_tracking(url: str) -> str:
    """Убираем utm_* и трекинг LinkedIn-шеринга из ссылки."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
         if not k.lower().startswith("utm_") and k.lower() not in {"rcm", "trk", "trackingid", "refid"}]
    return urlunsplit(parts._replace(query=urlencode(q)))


def _tg_links(v: Vacancy) -> list[dict]:
    """Ссылки из TG-поста без ссылок на сам канал, инвайтов и превью."""
    ch = ((v.raw or {}).get("channel") or "").lower()
    out = []
    for link in (v.raw or {}).get("links") or []:
        href = (link.get("href") or "").strip()
        low = href.lower()
        if not href.startswith("http"):
            continue
        if ch and (low.rstrip("/").endswith(f"t.me/{ch}") or f"t.me/{ch}/" in low or f"t.me/s/{ch}" in low):
            continue
        if "t.me/+" in low or "joinchat" in low:
            continue
        out.append(link)
    return out


# ─── 1. сбор ───

# Результаты LinkedIn из утреннего цикла — переиспользуем в дневных, не дёргая LinkedIn повторно.
# Живут в памяти процесса: после рестарта сервиса кэш пуст до следующего утреннего цикла.
_LINKEDIN_CACHE: list[Vacancy] = []


async def collect(fetch_linkedin: bool = True) -> list[Vacancy]:
    global _LINKEDIN_CACHE
    cs, tg = CareerSiteSource(), TelegramChannelSource()
    sem = asyncio.Semaphore(8)

    async def one(fetcher, ident):
        async with sem:
            src = SimpleNamespace(identifier=ident, is_active=True, user_id=None)
            try:
                return await asyncio.wait_for(fetcher.fetch(src), timeout=180)
            except Exception as e:  # noqa: BLE001
                logger.warning("channel_source_failed %s: %s", ident, str(e)[:200])
                return []

    site_ids = [s for s in get_career_site_ids() if fetch_linkedin or s != "linkedin"]
    tasks = [one(cs, s) for s in site_ids]
    tasks += [one(tg, c) for c in dict.fromkeys(TELEGRAM_CHANNELS)]
    results = await asyncio.gather(*tasks)
    vacancies = [v for batch in results for v in batch]

    if fetch_linkedin:
        _LINKEDIN_CACHE = [v for v in vacancies if _source(v) == "linkedin"]
    else:
        vacancies += _LINKEDIN_CACHE
    return vacancies


# ─── 2. сито правилами + дедуп ───

async def _already_seen(hashes: list[str], fps: list[str]) -> set[str]:
    if not hashes:
        return set()
    async with async_session() as session:
        rows = await session.execute(
            text("SELECT vacancy_hash, fingerprint FROM posted_to_channel "
                 "WHERE vacancy_hash = ANY(:h) OR fingerprint = ANY(:f)"),
            {"h": hashes, "f": fps},
        )
        seen = set()
        for h, f in rows:
            seen.add(h)
            if f:
                seen.add(f)
        return seen


async def prefilter(vacancies: list[Vacancy]) -> tuple[list[Vacancy], Counter]:
    """Сито правилами + дедуп. Возвращает прошедших и счётчик причин отсева."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.MAX_AGE_DAYS)
    out, fps, cut = [], set(), Counter()
    for v in vacancies:
        fp = v.content_fingerprint
        if fp in fps:
            cut["дубль_в_сборе"] += 1
            continue
        fps.add(fp)
        if not cfg.in_niche(v.title, v.description):
            cut["не_ниша"] += 1
            continue
        if cfg.is_blacklisted(v.company):
            cut["чёрный_список"] += 1
            continue
        d = _parse_dt(v.published_at)
        if (d and d < cutoff) or (d is None and not _is_tg(v)):
            cut["старая_или_без_даты"] += 1
            continue
        if not _is_tg(v) and not cfg.geo_ok(v.location, v.description):
            cut["гео"] += 1
            continue
        if _is_tg(v) and not _tg_links(v):
            cut["tg_без_ссылки"] += 1
            continue
        out.append(v)

    seen = await _already_seen([v.hash for v in out], [v.content_fingerprint for v in out])
    fresh = [v for v in out if v.hash not in seen and v.content_fingerprint not in seen]
    cut["уже_было_в_канале"] = len(out) - len(fresh)
    return fresh, cut


BOARDS = {"remoteok", "remotive", "habr_career", "hirehi", "dreamjob"}


def _group(v: Vacancy) -> str:
    """Группа источника для честного деления мест на оценку."""
    src = _source(v)
    if src.startswith("tg:"):
        return "tg"
    if src in ("linkedin", "getmatch", "himalayas"):
        return src
    if src in BOARDS or src.startswith("wwr"):
        return "boards"
    return "sites"


def _round_robin(queues: list[list]) -> list:
    out, queues = [], [q for q in queues if q]
    while queues:
        for q in list(queues):
            out.append(q.pop(0))
            if not q:
                queues.remove(q)
    return out


def cap_round_robin(vacancies: list[Vacancy], cap: int) -> list[Vacancy]:
    """Места на оценку делим поровну между группами (linkedin, getmatch, himalayas, tg, boards, sites),
    внутри группы — по кругу между источниками, свежие первыми."""
    newest = lambda x: _parse_dt(x.published_at) or datetime.min.replace(tzinfo=timezone.utc)  # noqa: E731
    by_group: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for v in vacancies:
        by_group[_group(v)][_source(v)].append(v)

    group_queues = []
    for sources in by_group.values():
        for q in sources.values():
            q.sort(key=newest, reverse=True)
        group_queues.append(_round_robin(list(sources.values())))

    return _round_robin(group_queues)[:cap]


# ─── 3. оценка Haiku ───

def _brief(i: int, v: Vacancy) -> str:
    return (f"[{i}] {v.title} | {v.company or '-'} | {v.location or '-'} | {v.salary or '-'} "
            f"| источник: {_source(v)}\n{(v.description or '')[:400]}")


async def score(vacancies: list[Vacancy]) -> list[tuple[Vacancy, dict]]:
    scored = []
    for start in range(0, len(vacancies), cfg.SCORE_BATCH):
        batch = vacancies[start:start + cfg.SCORE_BATCH]
        user = "\n\n".join(_brief(i, v) for i, v in enumerate(batch))
        try:
            raw = await _haiku(SCORE_SYSTEM, user, max_tokens=2000)
        except Exception as e:  # noqa: BLE001
            logger.warning("channel_score_failed: %s", str(e)[:200])
            continue
        items = _extract_json(raw, "[") or []
        by_i = {it.get("i"): it for it in items if isinstance(it, dict)}
        for i, v in enumerate(batch):
            if i in by_i:
                scored.append((v, by_i[i]))
    return scored


def select(scored: list[tuple[Vacancy, dict]]) -> list[tuple[Vacancy, dict]]:
    ok = [(v, s) for v, s in scored if s.get("post") and (s.get("score") or 0) >= cfg.MIN_SCORE]
    ok.sort(key=lambda x: x[1].get("score", 0), reverse=True)
    picked, per_company, per_source = [], Counter(), Counter()
    for v, s in ok:
        comp, src = (v.company or "").strip().lower(), _source(v)
        if comp and per_company[comp] >= 1:
            continue
        if per_source[src] >= _source_limit(src):
            continue
        picked.append((v, s))
        per_company[comp] += 1
        per_source[src] += 1
        if len(picked) >= cfg.POSTS_PER_RUN:
            break
    return picked


# ─── 4. карточка ───

async def enrich_linkedin(v: Vacancy) -> Vacancy:
    job_id = (v.raw or {}).get("job_id")
    if not job_id:
        return v
    url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{job_id}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(url, headers=UA)
        if r.status_code != 200:
            return v
        el = BeautifulSoup(r.text, "html.parser").select_one("div.show-more-less-html__markup")
        if el:
            return dataclasses.replace(v, description=el.get_text(" ", strip=True)[:3000])
    except httpx.HTTPError:
        pass
    return v


def _render_tags(data: dict) -> str:
    """Только разрешённые теги, в фиксированном порядке групп, без дублей."""
    chosen = {str(t).strip().lstrip("#").lower() for t in (data.get("tags") or [])}
    ordered = [t for group in (cfg.TAGS_FORMAT, cfg.TAGS_ROLE, cfg.TAGS_GEO) for t in group if t in chosen]
    if data.get("salary"):
        ordered.append(cfg.TAG_SALARY)
    return " ".join(f"#{t}" for t in ordered)


async def make_card(v: Vacancy) -> str | None:
    if _source(v) == "linkedin":
        v = await enrich_linkedin(v)
    company_hint = "-" if _is_tg(v) else (v.company or "-")  # у TG в company лежит имя канала
    user = (f"Название: {v.title}\nКомпания: {company_hint}\nЛокация: {v.location or '-'}\n"
            f"Зарплата: {v.salary or '-'}\n\nТекст вакансии:\n{(v.description or '')[:3000]}")
    links = _tg_links(v) if _is_tg(v) else []
    if links:
        user += "\n\nСсылки из поста:\n" + "\n".join(
            f"[{i}] «{l.get('text') or ''}» — {l['href']}" for i, l in enumerate(links)
        )
    try:
        raw = await _haiku(CARD_SYSTEM, user, max_tokens=500)
    except Exception as e:  # noqa: BLE001
        logger.warning("channel_card_failed: %s", str(e)[:200])
        return None
    data = _extract_json(raw, "{")
    if not data or not data.get("post") or not data.get("role") or not data.get("summary"):
        return None

    apply_url = html.unescape(v.url)
    if _is_tg(v):
        idx = data.get("apply")
        if not isinstance(idx, int) or not (0 <= idx < len(links)):
            return None  # не нашли, куда откликаться — не постим
        apply_url = html.unescape(links[idx]["href"])
    apply_url = _strip_tracking(apply_url)

    e = lambda s: html.escape(str(s))  # noqa: E731
    lines = [f"🎯 <b>{e(data['role'])}</b>"]
    meta = " · ".join(x for x in [data.get("sphere"), data.get("format")] if x)
    if meta:
        lines.append(e(meta))
    cs = " · ".join(x for x in [
        f"🏢 {data['company']}" if data.get("company") else None,
        f"💰 {data['salary']}" if data.get("salary") else None,
    ] if x)
    if cs:
        lines.append(e(cs))
    lines += ["", e(data["summary"]), ""]
    tags_line = _render_tags(data)
    lines.append(f'→ <a href="{e(apply_url)}">Откликнуться</a>')
    if tags_line:
        lines += ["", tags_line]
    return "\n".join(lines)


# ─── 5. запись в БД и публикация ───

async def _save(v: Vacancy, s: dict, status: str, message_id: int | None = None):
    async with async_session() as session:
        await session.execute(
            text("INSERT INTO posted_to_channel "
                 "(vacancy_hash, fingerprint, source, title, url, score, status, reason, message_id) "
                 "VALUES (:h, :f, :src, :t, :u, :sc, :st, :r, :m) "
                 "ON CONFLICT (vacancy_hash) DO NOTHING"),
            {"h": v.hash, "f": v.content_fingerprint, "src": _source(v), "t": v.title[:500],
             "u": v.url, "sc": s.get("score"), "st": status, "r": (s.get("reason") or "")[:300],
             "m": message_id},
        )
        await session.commit()


async def _save_run(st: dict) -> None:
    """Одна строка в channel_runs на каждый боевой цикл (и на упавший тоже)."""
    try:
        async with async_session() as session:
            await session.execute(
                text("INSERT INTO channel_runs (started_at, finished_at, with_linkedin, collected, "
                     "after_sieve, sieve_cut, to_score, by_group, scored, approved, quality, picked, "
                     "posted, card_failed, send_failed, posted_sources, error) VALUES "
                     "(:started_at, :finished_at, :with_linkedin, :collected, :after_sieve, "
                     "CAST(:sieve_cut AS JSONB), :to_score, CAST(:by_group AS JSONB), :scored, :approved, "
                     ":quality, :picked, :posted, :card_failed, :send_failed, "
                     "CAST(:posted_sources AS JSONB), :error)"),
                {**st, "sieve_cut": json.dumps(st["sieve_cut"], ensure_ascii=False),
                 "by_group": json.dumps(st["by_group"], ensure_ascii=False),
                 "posted_sources": json.dumps(st["posted_sources"], ensure_ascii=False)},
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001 — статистика не должна ронять цикл
        logger.warning("channel_run_stats_failed: %s", str(e)[:200])


async def _send(bot, card: str) -> int | None:
    from aiogram.exceptions import TelegramRetryAfter
    for _ in range(3):
        try:
            msg = await bot.send_message(cfg.CHANNEL_ID, card, parse_mode="HTML",
                                         disable_web_page_preview=True)
            return msg.message_id
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
    return None


async def run_channel_cycle(bot=None, dry_run: bool = True, fetch_linkedin: bool = True) -> None:
    st = {"started_at": datetime.now(timezone.utc), "finished_at": None, "with_linkedin": fetch_linkedin,
          "collected": 0, "after_sieve": 0, "sieve_cut": {}, "to_score": 0, "by_group": {},
          "scored": 0, "approved": 0, "quality": 0, "picked": 0, "posted": 0,
          "card_failed": 0, "send_failed": 0, "posted_sources": {}, "error": None}
    try:
        await _cycle(bot, dry_run, fetch_linkedin, st)
    except Exception as e:  # noqa: BLE001
        st["error"] = f"{type(e).__name__}: {str(e)[:500]}"
        logger.exception("channel_cycle_failed")
        raise
    finally:
        st["finished_at"] = datetime.now(timezone.utc)
        print(f"═══ канал: собрано {st['collected']} → сито {st['after_sieve']} → на оценку {st['to_score']} "
              f"→ оценено {st['scored']} → post=true {st['approved']} → качественных {st['quality']} "
              f"→ выбрано {st['picked']} → опубликовано {st['posted']} | отсев: {st['sieve_cut']} "
              f"| группы: {st['by_group']}" + (f" | ОШИБКА: {st['error']}" if st["error"] else ""),
              flush=True)
        if not dry_run:
            await _save_run(st)


async def _cycle(bot, dry_run: bool, fetch_linkedin: bool, st: dict) -> None:
    raw = await collect(fetch_linkedin=fetch_linkedin)
    st["collected"] = len(raw)
    filtered, cut = await prefilter(raw)
    st["after_sieve"], st["sieve_cut"] = len(filtered), dict(cut)
    candidates = cap_round_robin(filtered, cfg.CANDIDATES_CAP)
    st["to_score"], st["by_group"] = len(candidates), dict(Counter(_group(v) for v in candidates))
    scored = await score(candidates)
    st["scored"] = len(scored)
    st["approved"] = sum(1 for _, s in scored if s.get("post"))
    st["quality"] = sum(1 for _, s in scored if s.get("post") and (s.get("score") or 0) >= cfg.MIN_SCORE)
    picked = select(scored)
    st["picked"] = len(picked)

    posted_sources = Counter()
    for n, (v, s) in enumerate(picked):
        card = await make_card(v)
        if not card:
            st["card_failed"] += 1
            print(f"✗ карточка не собралась: {v.title[:60]} ({_source(v)})", flush=True)
            if not dry_run:
                await _save(v, s, "rejected")
            continue

        if dry_run:
            print(f"── [{s.get('score')}] {_source(v)} · {s.get('reason')}\n{card}\n", flush=True)
            continue

        message_id = await _send(bot, card)
        await _save(v, s, "posted" if message_id else "failed", message_id)
        if message_id:
            st["posted"] += 1
            posted_sources[_source(v)] += 1
        else:
            st["send_failed"] += 1
        if n < len(picked) - 1:  # после последнего поста не ждём
            await asyncio.sleep(cfg.PAUSE_BETWEEN_POSTS)
    st["posted_sources"] = dict(posted_sources)

    if not dry_run:
        picked_ids = {id(v) for v, _ in picked}
        for v, s in scored:
            if id(v) not in picked_ids and (not s.get("post") or (s.get("score") or 0) < cfg.MIN_SCORE):
                await _save(v, s, "rejected")
