"""Career page scrapers for individual companies."""

from __future__ import annotations

import re
from typing import Callable, Awaitable

import httpx
from bs4 import BeautifulSoup, Tag

from src.db.models import Source, SourceType
from src.sources.base import JobSource, Vacancy

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# ---------- registry ----------

# site_id -> (display name, base url, parser function)
ParserFn = Callable[[str, str], list[Vacancy]]

# ════════════════════════════════════════════════════════════════════
# Registry — теперь поддерживает оба типа парсеров: HTML и API
# ════════════════════════════════════════════════════════════════════

def _registry() -> dict[str, tuple]:
    """site_id -> (display_name, fetch_kind, *args).

    fetch_kind:
      - "html": ходим GET-ом на URL, передаём HTML в parser_fn
      - "greenhouse": универсальный Greenhouse API, args = (company_slug,)
      - "teamtailor": универсальный Teamtailor HTML, args = (url, site_id)
    """
    return {
        # ─── уже существующие HTML-парсеры ───
        "tochka": ("Точка Банк", "html", "https://hr.tochka.com/vacancies/", _parse_tochka),
        "indrive": ("inDrive", "html", "https://careers.indrive.com/vacancies/", _parse_indrive),
        "aviasales": ("Aviasales", "html", "https://www.aviasales.ru/about/vacancies", _parse_aviasales),
        "garage_eight": ("Garage Eight", "html", "https://garage-eight.com/vacancies/", _parse_garage_eight),
        "uzum": ("Uzum", "html", "https://people.uzum.com/career/ru/vacancies", _parse_uzum),
        "avito": ("Avito", "html", "https://career.avito.com/vacancies/", _parse_avito),
        "vk_company": ("VK Company", "html", "https://team.vk.company/vacancy/", _parse_vk_company),
        "yandex": ("Яндекс", "html", "https://yandex.ru/jobs/vacancies", _parse_yandex),
        "logika_moloka": ("Логика Молока", "html", "https://career.logikamoloka.ru/vacancies/", _parse_logika_moloka),
        "mvideo": ("М.Видео", "html", "https://career.mvideoeldorado.ru/vacancies", _parse_mvideo),

        # ─── Greenhouse API: лёгко добавить любую компанию из их экосистемы ───
        "anthropic": ("Anthropic", "greenhouse", "anthropic"),
        "stripe": ("Stripe", "greenhouse", "stripe"),
        "figma": ("Figma", "greenhouse", "figma"),
        "mercury": ("Mercury", "greenhouse", "mercury"),
        "datadog": ("Datadog", "greenhouse", "datadog"),
        "vercel": ("Vercel", "greenhouse", "vercel"),
        "duolingo": ("Duolingo", "greenhouse", "duolingo"),
        "coursera": ("Coursera", "greenhouse", "coursera"),

        # ─── Teamtailor HTML ───
        "sumsub": ("Sumsub", "teamtailor", "https://careers.sumsub.com/jobs", "sumsub"),

        # ─── кастомный HTML ───
        "zalando": ("Zalando", "html", "https://jobs.zalando.com/en/jobs", _parse_zalando),
    }


class CareerSiteSource(JobSource):
    """Dispatch к разным типам fetcher'ов через _registry."""

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def fetch(self, source: Source) -> list[Vacancy]:
        entry = _registry().get(source.identifier)
        if entry is None:
            return []

        display_name, kind, *args = entry

        if kind == "html":
            url, parser = args
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                try:
                    response = await client.get(url, headers={"User-Agent": USER_AGENT})
                    if response.status_code != 200:
                        return []
                    return parser(response.text, url)
                except httpx.HTTPError:
                    return []

        if kind == "greenhouse":
            (company_slug,) = args
            return await _fetch_greenhouse(company_slug, display_name)

        if kind == "teamtailor":
            url, site_id = args
            return await _fetch_teamtailor(url, display_name, site_id)

        return []


def get_career_site_ids() -> list[str]:
    return list(_registry().keys())


def get_company_name(site_id: str) -> str | None:
    entry = _registry().get(site_id)
    return entry[0] if entry else None

# ---------- per-site parsers ----------

def _parse_tochka(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для hr.tochka.com.

    Структура: ссылки на /vacancies/catalog/... + текст с локацией/зарплатой/опытом.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []

    for link in soup.find_all("a", href=re.compile(r"/vacancies/catalog/")):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue

        # tochka формат: "Название_должности | Категория | Локация | от/до X ₽ | Опыт"
        # склеено в одну строку, разделение определяем эвристически
        title, location, salary, experience = _split_tochka_text(text)

        # external_id из URL
        slug = href.rstrip("/").split("/")[-1]
        external_id = f"tochka:{slug}"

        full_url = href if href.startswith("http") else f"https://hr.tochka.com{href}"

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=title or "Вакансия",
                company="Точка Банк",
                url=full_url,
                description=text,
                salary=salary,
                location=location,
                published_at=None,
                raw={"site": "tochka", "raw_text": text},
            )
        )

    return vacancies


def _split_tochka_text(text: str) -> tuple[str, str | None, str | None, str | None]:
    """Из склеенной строки выделить title/location/salary/experience.

    Текст: "IT-аудиторРиски, комплаенс и аудитУдалённоот 300 000 ₽Более 5 лет"
    """
    salary_match = re.search(r"(?:от|до)?\s?\d{1,3}\s?\d{3}(?:[\s\xa0]?\d{3})?\s?[₽$€]", text)
    salary = salary_match.group(0).strip() if salary_match else None

    experience_match = re.search(
        r"(?:Без опыта|От \d+(?:\sдо\s\d+)?\s+(?:год|лет)\w*|Более \d+ лет)",
        text,
    )
    experience = experience_match.group(0) if experience_match else None

    location = None
    for city in ("Удалённо", "Москва", "Санкт-Петербург", "Сургут", "Краснодар", "Екатеринбург", "Гибрид", "Разъезды"):
        if city in text:
            location = city
            break

    # title — первое слово или несколько слов до первого "разделителя"
    # Используем найденные куски как маркеры конца title.
    title = text
    for marker in (salary, experience, location):
        if marker and marker in title:
            title = title.split(marker)[0]
    # category тоже отрезаем: после title часто идёт категория с заглавной буквы
    title = re.split(r"(?<=[а-яa-z])(?=[А-ЯA-Z])", title, maxsplit=1)[0].strip()
    return title[:200] or "Вакансия", location, salary, experience

def _parse_indrive(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для careers.indrive.com.

    Структура: ссылки на indrive.pinpointhq.com/en/postings/UUID + текст вакансии.
    Текст в формате 'Position Title Format - Time - Division Country'.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []

    for link in soup.find_all("a", href=re.compile(r"indrive\.pinpointhq\.com/.+/postings/")):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue

        # UUID из конца URL
        uuid = href.rstrip("/").split("/")[-1]
        external_id = f"indrive:{uuid}"

        # Разбор текста: ищем " - " как разделители
        # Пример: "Senior Marketing Manager Hybrid - Full Time - Growth Businesses Philippines"
        parts = [p.strip() for p in text.split(" - ")]
        if len(parts) >= 3:
            # Первая часть содержит title + format (Hybrid/Remote/On-site)
            title_part = parts[0]
            employment = parts[1] if len(parts) > 1 else None
            location_division = parts[-1]

            # Отделить format от title в первой части
            title = title_part
            location_format = None
            for fmt in ("Hybrid", "Remote", "On-site", "On site"):
                if title_part.endswith(fmt):
                    title = title_part[: -len(fmt)].strip()
                    location_format = fmt
                    break

            location = location_division
            if location_format:
                location = f"{location_format}, {location_division}"
        else:
            title = text
            location = None
            employment = None

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=title[:200] or "Position",
                company="inDrive",
                url=href if href.startswith("http") else f"https:{href}",
                description=text,
                salary=None,  # inDrive не публикует на этой странице
                location=location,
                published_at=None,
                raw={"site": "indrive", "raw_text": text, "employment": employment},
            )
        )

    return vacancies

def _parse_aviasales(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для aviasales.ru/about/vacancies.

    Структура: ссылки на /about/vacancies/{ID} + текст вакансии с категорией.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []

    for link in soup.find_all("a", href=re.compile(r"/about/vacancies/\d+")):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 5:
            continue

        # ID из URL
        match = re.search(r"/vacancies/(\d+)", href)
        if not match:
            continue
        external_id = f"aviasales:{match.group(1)}"

        full_url = href if href.startswith("http") else f"https://www.aviasales.ru{href}"

        # Текст вида: "B2B: Sales Business Development Manager (Enterprise) InboundOutbound"
        # Первая часть до тегов навыков (часто слипшиеся CamelCase в конце)
        # Разделим CamelCase в конце как теги, остальное — title
        title = re.split(r"(?<=[а-яa-z])(?=[А-ЯA-Z])", text)
        # title[0] обычно содержит саму вакансию вместе с разделом
        clean_title = title[0].strip() if title else text

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=clean_title[:200] or "Vacancy",
                company="Aviasales",
                url=full_url,
                description=text,
                salary=None,
                location=None,  # Aviasales не указывает на странице списка
                published_at=None,
                raw={"site": "aviasales", "raw_text": text},
            )
        )

    return vacancies

def _parse_garage_eight(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для garage-eight.com/vacancies.

    Структура: ссылки на /vacancy/SLUG + текст с скиллами и локацией.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []

    for link in soup.find_all("a", href=re.compile(r"/vacancy/[a-z0-9\-]+/?$")):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 10:
            continue

        slug = href.rstrip("/").split("/")[-1]
        external_id = f"garage_eight:{slug}"

        full_url = href if href.startswith("http") else f"https://garage-eight.com{href}"

        # Текст вида: "Process manager OKR, Change management Санкт-Петербург"
        # Локация в конце — известный список городов
        location = None
        for city in ("Санкт-Петербург", "Москва", "Удалённо", "Гибрид"):
            if city in text:
                location = city
                break

        # Title — до первого знака пунктуации или до локации
        title = text
        if location and location in title:
            title = title.split(location)[0]
        # Чистим хвостовые навыки после запятой/слэша
        title = re.split(r"[,/]", title, maxsplit=1)[0].strip()

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=title[:200] or "Вакансия",
                company="Garage Eight",
                url=full_url,
                description=text,
                salary=None,
                location=location,
                published_at=None,
                raw={"site": "garage_eight", "raw_text": text},
            )
        )

    return vacancies


def _parse_uzum(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для people.uzum.com/career/ru/vacancies.

    У Uzum ссылка <a> пустая, текст вакансии живёт в родительском контейнере.
    Структура контейнера: категория, команда, h3 с title, метаданные (город • формат • опыт).
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []

    for link in soup.find_all("a", href=re.compile(r"/career/ru/vacancies/\d+")):
        href = link.get("href", "")
        match = re.search(r"/vacancies/(\d+)", href)
        if not match:
            continue
        external_id = f"uzum:{match.group(1)}"

        # Поднимаемся к контейнеру вакансии. У Uzum это li или ближайший div с h3 внутри.
        container = link.find_parent("li") or link.find_parent("div")
        if container is None:
            continue

        # Title — из h3 внутри контейнера
        title_tag = container.find(["h3", "h2"])
        title = title_tag.get_text(strip=True) if title_tag else None

        # Полный текст контейнера для description и метаданных
        full_text = container.get_text(" ", strip=True)
        if not full_text or len(full_text) < 10:
            continue

        if not title:
            # Fallback: возьмём первую "значимую" строку из текста
            title = full_text[:120]

        # Локация и формат работы из текста
        location = None
        city_match = re.search(
            r"(Ташкент|Москва|Санкт-Петербург|Самарканд|Бухара|Нукус|Ургенч|Андижан|Фергана|Наманган|Карши|Термез|Джизак)",
            full_text,
        )
        if city_match:
            location = city_match.group(1)

        work_format = None
        for fmt in ("Удалённая работа", "Гибридный", "Офис"):
            if fmt in full_text:
                work_format = fmt
                break
        if location and work_format:
            location = f"{location}, {work_format}"
        elif work_format:
            location = work_format

        full_url = href if href.startswith("http") else f"https://people.uzum.com{href}"

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=title[:200] or "Вакансия",
                company="Uzum",
                url=full_url,
                description=full_text,
                salary=None,
                location=location,
                published_at=None,
                raw={"site": "uzum", "raw_text": full_text},
            )
        )

    return vacancies

def _parse_avito(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для career.avito.com/vacancies/.

    Структура: каждая вакансия — две ссылки на /vacancies/{direction}/{id}/
    (одна иконка-картинка, одна с текстом). Берём ту, у которой есть текст.
    Локация и формат работы — соседние элементы в родительском контейнере.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []
    seen_ids: set[str] = set()

    link_pattern = re.compile(r"^/vacancies/([a-z\-]+)/(\d+)/?$")

    for link in soup.find_all("a", href=link_pattern):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text:
            continue  # это пустая ссылка-иконка, пропускаем

        match = link_pattern.match(href)
        if not match:
            continue
        direction, vacancy_id = match.group(1), match.group(2)
        if vacancy_id in seen_ids:
            continue
        seen_ids.add(vacancy_id)

        external_id = f"avito:{vacancy_id}"
        full_url = f"https://career.avito.com{href}"

        # Локация и формат — следующие элементы у родительского блока
        location, work_format, team = _extract_avito_meta(link)

        # Description складываем из всего, что есть — нужно matcher'у для оценки
        description_parts = [text]
        if direction:
            description_parts.append(f"Направление: {direction.replace('-', ' ')}")
        if team:
            description_parts.append(f"Команда: {team}")
        if location:
            description_parts.append(f"Локация: {location}")
        if work_format:
            description_parts.append(f"Формат: {work_format}")
        description = "\n".join(description_parts)

        # Итоговая локация — комбинация города и формата
        final_location = None
        if location and work_format:
            final_location = f"{location}, {work_format}"
        elif location:
            final_location = location
        elif work_format:
            final_location = work_format

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=text[:200],
                company="Avito",
                url=full_url,
                description=description,
                salary=None,  # Avito не публикует зарплату на листинге
                location=final_location,
                published_at=None,
                raw={"site": "avito", "direction": direction, "team": team},
            )
        )

    return vacancies


def _extract_avito_meta(link_tag) -> tuple[str | None, str | None, str | None]:
    """Из родителя ссылки достаём локацию, формат работы и команду."""
    cities = {
        "москва", "санкт-петербург", "казань", "нижний новгород", "тула",
        "краснодар", "ростов-на-дону", "самара", "волгоград", "воронеж",
        "екатеринбург", "новосибирск", "удалённая работа",
    }
    formats = {
        "удалёнка", "офис", "гибрид", "гибрид или удалёнка",
        "разъездной", "гибрид подходит для людей с овз",
    }

    # Поднимаемся вверх по дереву, ищем контейнер вакансии
    container = link_tag.parent
    matched_container = None
    for _ in range(5):
        if container is None:
            break
        try:
            text = container.get_text(" ", strip=True).lower()
        except AttributeError:
            break
        if any(c in text for c in cities) or any(f in text for f in formats):
            matched_container = container
            break
        container = container.parent

    if matched_container is None:
        return None, None, None

    full_text = matched_container.get_text(" | ", strip=True)
    full_text_lower = full_text.lower()

    location = None
    for city in sorted(cities, key=len, reverse=True):
        idx = full_text_lower.find(city)
        if idx != -1:
            location = full_text[idx : idx + len(city)]
            break

    work_format = None
    for fmt in sorted(formats, key=len, reverse=True):
        idx = full_text_lower.find(fmt)
        if idx != -1:
            work_format = full_text[idx : idx + len(fmt)]
            break

    team = None
    try:
        team_link = matched_container.find("a", href=re.compile(r"^/teams/[a-z\-]+/?$"))
        if team_link:
            team = team_link.get_text(strip=True)
    except AttributeError:
        team = None

    return location, work_format, team

def _parse_vk_company(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для team.vk.company/vacancy/.

    Структура: <a href="/vacancy/XXXXX/">Title Team CityFormat</a>
    Текст слипшийся (CamelCase разделения нет), приходится резать эвристиками.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []
    seen_ids: set[str] = set()

    link_pattern = re.compile(r"^/vacancy/(\d+)/?$")

    # Известные форматы работы — конец строки
    formats = [
        "офисный", "гибкий", "комбинированный", "удалённый", "удаленный",
        "разъездной", "дистанционный",
    ]
    # Известные города — перед форматом
    cities = [
        "Москва", "Санкт-Петербург", "Астана", "Волгоград",
        "Алматы", "Минск", "Сочи", "Казань",
    ]

    for link in soup.find_all("a", href=link_pattern):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 5:
            continue

        match = link_pattern.match(href)
        if not match:
            continue
        vacancy_id = match.group(1)
        if vacancy_id in seen_ids:
            continue
        seen_ids.add(vacancy_id)

        external_id = f"vk_company:{vacancy_id}"
        full_url = f"https://team.vk.company{href}"

        # Извлекаем формат — обычно после запятой в конце
        work_format = None
        text_for_title = text
        for fmt in formats:
            if text.lower().endswith(fmt):
                work_format = fmt
                text_for_title = text[: -len(fmt)].rstrip(", ").strip()
                break

        # Извлекаем город перед форматом
        location = None
        for city in cities:
            # Город обычно идёт через запятую перед форматом
            pattern = re.compile(rf"\b{re.escape(city)}\b")
            city_match = pattern.search(text_for_title)
            if city_match:
                location = city
                text_for_title = text_for_title[: city_match.start()].rstrip(", ").strip()
                break

        # Что осталось — title + команда (CamelCase, слипшиеся)
        # Команда обычно с заглавной буквы после первой строчной
        # Разделяем по переходу строчная→Заглавная
        parts = re.split(r"(?<=[а-яa-z])(?=[А-ЯA-Z])", text_for_title, maxsplit=1)
        title = parts[0].strip() if parts else text_for_title
        team = parts[1].strip() if len(parts) > 1 else None

        # Финальная локация — комбинация города и формата
        final_location = None
        if location and work_format:
            final_location = f"{location}, {work_format}"
        elif location:
            final_location = location
        elif work_format:
            final_location = work_format

        # Description: соберём всё что есть для матчера
        description_parts = [title]
        if team:
            description_parts.append(f"Команда: {team}")
        if final_location:
            description_parts.append(f"Локация: {final_location}")
        description = "\n".join(description_parts)

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=title[:200] or "Вакансия",
                company="VK Company",
                url=full_url,
                description=description,
                salary=None,
                location=final_location,
                published_at=None,
                raw={"site": "vk_company", "team": team},
            )
        )

    return vacancies


def _parse_yandex(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для yandex.ru/jobs/vacancies.

    На карточке Яндекса title живёт НЕ в h-заголовке, а отдельной строкой
    между блоком метаданных (сервис, город, формат) и описанием.
    Берём самую длинную строку из карточки длиной 20–200 символов,
    которая не совпадает с уже известными "не-title" вещами.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []
    seen_ids: set[str] = set()

    link_pattern = re.compile(r"^/jobs/vacancies/([a-z0-9\-]+?)-(\d+)/?$")

    formats_list = ["офис", "гибридный формат", "удалённая работа", "удаленная работа"]
    cities_list = [
        "Москва", "Санкт-Петербург", "Екатеринбург", "Новосибирск",
        "Казань", "Нижний Новгород", "Сасово", "Воронеж", "Тула",
        "Самара", "Сочи", "Уфа", "Иннополис", "Белград", "Минск",
    ]
    # Подстроки, по которым понимаем, что это НЕ title, а метаданные
    metadata_markers = [
        "ещё", "технолог", "город", "москв", "санкт-петер", "офис",
        "гибрид", "удалённ", "удаленн", "общие сервис", "поиск с алис",
    ]

    for link in soup.find_all("a", href=link_pattern):
        href = link.get("href", "")
        match = link_pattern.match(href)
        if not match:
            continue
        slug, vacancy_id = match.group(1), match.group(2)
        if vacancy_id in seen_ids:
            continue
        seen_ids.add(vacancy_id)

        external_id = f"yandex:{vacancy_id}"
        full_url = f"https://yandex.ru{href}"

        # Поднимаемся к карточке: контейнер с длинным текстом (>200 символов)
        card = link.parent
        for _ in range(6):
            if card is None:
                break
            text_len = len(card.get_text(" ", strip=True))
            if text_len > 200:
                break
            card = card.parent
        if card is None:
            continue

        # Все строки карточки
        lines = [
            l.strip()
            for l in card.get_text("\n", strip=True).split("\n")
            if l.strip()
        ]

        # Service — линк на /jobs/services/.../about
        service = None
        service_link = card.find("a", href=re.compile(r"^/jobs/services/[a-z0-9_\-]+/about"))
        if service_link:
            service = service_link.get_text(strip=True)

        # Title — самая длинная строка, которая:
        # - длиной 20–200 символов
        # - не метаданные (нет маркеров типа "Москва", "офис", "ещё 1")
        # - не название сервиса
        title_candidates = []
        for line in lines:
            line_lower = line.lower()
            if not (20 < len(line) < 200):
                continue
            if service and line == service:
                continue
            if any(m in line_lower for m in metadata_markers):
                continue
            title_candidates.append(line)

        if title_candidates:
            # Берём первую подходящую строку — обычно она и есть заголовок
            title = title_candidates[0]
        else:
            # Fallback: самая длинная строка карточки до 250 символов
            title = max((l for l in lines if len(l) < 250), key=len, default="Вакансия Яндекса")

        full_text = card.get_text(" ", strip=True)

        # Локация и формат
        location = next((c for c in cities_list if c in full_text), None)
        work_format = next(
            (f for f in formats_list if f in full_text.lower()), None
        )

        final_location = None
        if location and work_format:
            final_location = f"{location}, {work_format}"
        elif location:
            final_location = location
        elif work_format:
            final_location = work_format

        # Description: title + сервис + локация + тело
        description_parts = [title]
        if service:
            description_parts.append(f"Сервис: {service}")
        if final_location:
            description_parts.append(f"Локация: {final_location}")
        body = full_text.replace(title, "", 1).strip()
        if body and len(body) > 30:
            description_parts.append(body[:1500])
        description = "\n".join(description_parts)

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=title[:200],
                company="Яндекс",
                url=full_url,
                description=description,
                salary=None,
                location=final_location,
                published_at=None,
                raw={"site": "yandex", "service": service, "slug": slug},
            )
        )

    return vacancies

def _parse_logika_moloka(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для career.logikamoloka.ru/vacancies/.

    Структура: <a href="/vacancies/{slug}-{id}/"> с двумя соседними блоками —
    локация (один из городов) и название должности.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []
    seen_ids: set[str] = set()

    link_pattern = re.compile(r"^/vacancies/([a-z0-9_\-]+?)-(\d+)/?$")

    for link in soup.find_all("a", href=link_pattern):
        href = link.get("href", "")
        match = link_pattern.match(href)
        if not match:
            continue
        slug, vacancy_id = match.group(1), match.group(2)
        if vacancy_id in seen_ids:
            continue
        seen_ids.add(vacancy_id)

        external_id = f"logika_moloka:{vacancy_id}"
        full_url = f"https://career.logikamoloka.ru{href}"

        # Поднимаемся к контейнеру вакансии — у Логики структура простая,
        # обычно достаточно подняться на 1-2 уровня
        card = link.parent
        for _ in range(3):
            if card is None:
                break
            text_len = len(card.get_text(" ", strip=True))
            if text_len > 30:
                break
            card = card.parent
        if card is None:
            continue

        full_text = card.get_text(" | ", strip=True)
        lines = [
            l.strip()
            for l in card.get_text("\n", strip=True).split("\n")
            if l.strip() and l.strip().lower() != "откликнуться"
        ]

        # Город — отдельная короткая строка из известного списка
        cities = [
            "Москва", "Санкт-Петербург", "Кемерово", "Ялуторовск",
            "Липецк", "Самара", "Сургут", "Краснодар", "Екатеринбург",
            "Новосибирск", "Воронеж", "Нижний Новгород", "Казань",
            "Уфа", "Тюмень", "Челябинск", "Пермь", "Омск", "Ростов-на-Дону",
            "Ижевск", "Калининград", "Ярославль",
        ]
        location = next((c for c in cities if c in full_text), None)

        # Title — обычно строка длиной 10-100 символов, не город, не дата
        date_pattern = re.compile(
            r"\d{1,2}\s+(?:янв|фев|мар|апр|мая|июн|июл|авг|сен|окт|ноя|дек)",
            re.IGNORECASE,
        )
        title_candidates = []
        for line in lines:
            if location and line == location:
                continue
            if date_pattern.search(line):
                continue
            if 10 < len(line) < 200:
                title_candidates.append(line)
        title = title_candidates[0] if title_candidates else "Вакансия"

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=title[:200],
                company="Логика Молока",
                url=full_url,
                description=full_text,
                salary=None,
                location=location,
                published_at=None,
                raw={"site": "logika_moloka", "slug": slug},
            )
        )

    return vacancies

def _parse_mvideo(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для career.mvideoeldorado.ru/vacancies.

    Каждая вакансия — одна ссылка, текст слепленный из 4-5 частей без разделителей:
    адрес (длинный), название должности, дата ("DD месяц"), категория, зарплата (опционально).
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []
    seen_ids: set[str] = set()

    link_pattern = re.compile(r"^/vacancies/([a-f0-9]{20,})/?$")

    categories = ["Магазин", "Склад и Логистика", "Офис", "Колл-центр", "ИТ", "Стажировка"]
    date_pattern = re.compile(
        r"(\d{1,2})\s*(январ|феврал|март|апрел|мая|май|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*",
        re.IGNORECASE,
    )
    salary_pattern = re.compile(r"(\d{2,3}\s?\d{3})\s*[₽$€]")

    for link in soup.find_all("a", href=link_pattern):
        href = link.get("href", "")
        match = link_pattern.match(href)
        if not match:
            continue
        vacancy_id = match.group(1)
        if vacancy_id in seen_ids:
            continue
        seen_ids.add(vacancy_id)

        text = link.get_text(" ", strip=True)
        if not text or len(text) < 20:
            continue

        external_id = f"mvideo:{vacancy_id}"
        full_url = f"https://career.mvideoeldorado.ru{href}"

        # Зарплата
        salary_match = salary_pattern.search(text)
        salary = salary_match.group(0).strip() if salary_match else None

        # Категория — находим один из известных вариантов
        category = next((c for c in categories if c in text), None)

        # Дата вакансии — отсекаем как маркер, отделяющий title от категории
        date_match = date_pattern.search(text)

        # Сначала отрезаем всё ПОСЛЕ даты — там обычно категория и зарплата.
        # ДО даты живут адрес + название должности.
        if date_match:
            before_date = text[: date_match.start()].strip()
        else:
            # Fallback: режем по категории
            if category and category in text:
                before_date = text.split(category)[0].strip()
            else:
                before_date = text

        # Адрес кончается там, где начинается название должности.
        # Эвристика: адрес содержит цифры, индекс, "ул.", "д.", "стр.", "пр-кт",
        # а название должности — нет. Идём с конца и отрезаем последнее
        # слитное слово, которое выглядит как должность.
        title = _extract_mvideo_title(before_date)

        # Локация — упрощённо: первый город из адреса, иначе сам адрес покороче
        location = _extract_mvideo_location(before_date.replace(title, "").strip())

        description_parts = [title]
        if category:
            description_parts.append(f"Категория: {category}")
        if location:
            description_parts.append(f"Адрес: {location}")
        if salary:
            description_parts.append(f"Зарплата: {salary}")
        description = "\n".join(description_parts)

        vacancies.append(
            Vacancy(
                external_id=external_id,
                source_type=SourceType.career_site,
                title=title[:200] or "Вакансия",
                company="М.Видео",
                url=full_url,
                description=description,
                salary=salary,
                location=location,
                published_at=None,
                raw={"site": "mvideo", "category": category, "raw_text": text},
            )
        )

    return vacancies


def _extract_mvideo_title(text: str) -> str:
    """Из 'адрес + название должности' извлекаем только название.

    Эвристика: разрезаем по концу адреса. Признаки конца адреса:
    запятая+пробел+заглавная буква, последний номер дома/строения,
    либо явный переход адресной части в нормальный текст.
    """
    # Если в строке есть "стр.X" или "д.X" — берём всё после последнего такого блока
    addr_markers = re.finditer(
        r"(?:стр\.?\s*\d+[а-я]?|д\.?\s*\d+[а-я]?|вл\.?\s*\d+[а-я]?|зд\.?\s*\d+[а-я]?|корп\.?\s*\d+[а-я]?)",
        text,
        flags=re.IGNORECASE,
    )
    last_end = 0
    for m in addr_markers:
        last_end = m.end()
    if last_end:
        candidate = text[last_end:].strip(" ,.")
        if 5 < len(candidate) < 200:
            return candidate

    # Fallback: ищем после индекса (6 цифр) первое значимое слово
    zip_match = re.search(r"\b\d{6}\b", text)
    if zip_match:
        # Берём всё после индекса, чистим от запятых
        candidate = text[zip_match.end():].strip(" ,.")
        # Внутри ещё может быть адрес — режем по последнему числу с буквой/точкой
        # Простой fallback: если строка длинная, берём последние 5-12 слов
        words = candidate.split()
        if len(words) > 4:
            candidate = " ".join(words[-min(len(words), 8):])
        if 5 < len(candidate) < 200:
            return candidate

    # Совсем fallback: берём последние 60-80 символов
    if len(text) > 80:
        return text[-80:].strip(" ,.")
    return text.strip(" ,.")


def _extract_mvideo_location(text: str) -> str | None:
    """Из адреса вычленяем город."""
    cities = [
        "Москва", "Санкт-Петербург", "Екатеринбург", "Казань", "Новосибирск",
        "Хабаровск", "Ярославль", "Владивосток", "Краснодар", "Самара",
        "Череповец", "Смоленск", "Подольск", "Саратов", "Кемерово",
        "Набережные Челны", "Чехов", "Лаишевский", "Бийск", "Курчатов",
        "Пенза", "Тюмень", "Иркутск", "Воронеж", "Барнаул", "Калининград",
    ]
    for city in cities:
        if city in text:
            return city
    return None

# ════════════════════════════════════════════════════════════════════
# Universal ATS fetchers
# ════════════════════════════════════════════════════════════════════

async def _fetch_greenhouse(company_slug: str, company_name: str) -> list[Vacancy]:
    """Универсальный фетчер для Greenhouse ATS через публичный JSON API.
    
    Покрывает сотни компаний: Anthropic, Stripe, Notion, Linear, Figma,
    Mercury, Plaid, Datadog, Duolingo, Coursera и так далее.
    """
    api_url = f"https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            response = await client.get(api_url, headers={"User-Agent": USER_AGENT})
            if response.status_code != 200:
                return []
            data = response.json()
        except (httpx.HTTPError, ValueError):
            return []

    vacancies: list[Vacancy] = []
    for job in data.get("jobs", []):
        job_id = str(job.get("id", ""))
        if not job_id:
            continue

        title = job.get("title", "").strip()
        location = (job.get("location") or {}).get("name", "").strip() or None
        url = job.get("absolute_url", "").strip()
        if not title or not url:
            continue

        # Description: HTML с тегами, надо очистить через BS4
        content_html = job.get("content", "")
        if content_html:
            content_soup = BeautifulSoup(content_html, "html.parser")
            description = content_soup.get_text(" ", strip=True)[:2500]
        else:
            description = title

        departments = job.get("departments") or []
        dept_names = ", ".join(d.get("name", "") for d in departments if d.get("name"))
        if dept_names:
            description = f"Отдел: {dept_names}\n{description}"

        vacancies.append(
            Vacancy(
                external_id=f"greenhouse_{company_slug}:{job_id}",
                source_type=SourceType.career_site,
                title=title[:200],
                company=company_name,
                url=url,
                description=description,
                salary=None,
                location=location,
                published_at=job.get("updated_at"),
                raw={"site": f"greenhouse_{company_slug}", "departments": dept_names},
            )
        )
    return vacancies


async def _fetch_teamtailor(url: str, company_name: str, site_id: str) -> list[Vacancy]:
    """Универсальный фетчер для Teamtailor ATS через парсинг HTML.
    
    Покрывает Sumsub и другие компании на Teamtailor.
    Структура: <a href="/jobs/{id}-{slug}">Title Category · Location · WorkMode</a>
    """
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        try:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
            if response.status_code != 200:
                return []
            html = response.text
        except httpx.HTTPError:
            return []

    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []
    seen_ids: set[str] = set()

    link_pattern = re.compile(r"/jobs/(\d+)-([a-z0-9\-]+)/?$")

    for link in soup.find_all("a", href=link_pattern):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text or len(text) < 5:
            continue

        match = link_pattern.search(href)
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        full_url = href if href.startswith("http") else f"https://careers.{site_id}.com{href}"

        # Структура: "Title Category · Location · WorkMode"
        # Разрезаем по " · " как разделителю
        parts = [p.strip() for p in text.split("·")]
        title_with_category = parts[0] if parts else text
        location = parts[1].strip() if len(parts) > 1 else None
        work_mode = parts[2].strip() if len(parts) > 2 else None

        # Title и категория слиплись — разрезаем по последнему пробелу перед известными категориями
        categories = [
            "Sales", "Technical", "Product", "Analytics", "Marketing",
            "Human Resources", "Legal", "Customer Operations", "Operations",
            "Engineering", "Design", "Finance", "Video Ident",
        ]
        title = title_with_category
        category = None
        for cat in sorted(categories, key=len, reverse=True):
            if title_with_category.endswith(cat):
                title = title_with_category[: -len(cat)].strip()
                category = cat
                break

        final_location = None
        if location and work_mode:
            final_location = f"{location}, {work_mode}"
        elif location:
            final_location = location
        elif work_mode:
            final_location = work_mode

        description_parts = [title]
        if category:
            description_parts.append(f"Категория: {category}")
        if final_location:
            description_parts.append(f"Локация: {final_location}")
        description = "\n".join(description_parts)

        vacancies.append(
            Vacancy(
                external_id=f"teamtailor_{site_id}:{job_id}",
                source_type=SourceType.career_site,
                title=title[:200] or "Position",
                company=company_name,
                url=full_url,
                description=description,
                salary=None,
                location=final_location,
                published_at=None,
                raw={"site": f"teamtailor_{site_id}", "category": category},
            )
        )
    return vacancies


# ════════════════════════════════════════════════════════════════════
# Custom HTML parsers — Zalando + Lesta
# ════════════════════════════════════════════════════════════════════

def _parse_zalando(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для jobs.zalando.com/en/jobs.
    
    Структура: <a href="/en/jobs/{id}-{slug}">TitleCategoryView jobLocationView job</a>
    Слова "View job" дублируются, надо вырезать.
    """
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []
    seen_ids: set[str] = set()

    link_pattern = re.compile(r"^/en/jobs/(\d+)-(.+)$")

    categories = [
        "Applied Science & Research", "Business Development & Strategy",
        "Customer Care", "Finance, Analytics & Controlling",
        "IT Consulting & Operations", "Logistics & Supply Chain",
        "Maintenance & Facility", "People & Organization",
        "Product Management", "Retail", "Backend", "Frontend",
    ]

    for link in soup.find_all("a", href=link_pattern):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text:
            continue

        match = link_pattern.match(href)
        if not match:
            continue
        job_id = match.group(1)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        # Чистим повторы "View job"
        text = re.sub(r"\s*View job\s*", " ", text).strip()

        # Категория обычно в конце title-куска
        category = None
        title = text
        for cat in sorted(categories, key=len, reverse=True):
            if cat in text:
                category = cat
                # Разделяем text на title и хвост
                idx = text.find(cat)
                title = text[:idx].strip()
                rest = text[idx + len(cat):].strip()
                location = rest if rest else None
                break
        else:
            location = None

        # Если категории не нашли, ищем известные города
        if not location:
            for city in ["Berlin", "Moenchengladbach", "Giessen", "Lahr", "Helsinki", "Dublin", "Remote Work", "Ansbach"]:
                if city in text:
                    location = city
                    title = text.split(city)[0].strip()
                    break

        full_url = f"https://jobs.zalando.com{href}"

        description_parts = [title]
        if category:
            description_parts.append(f"Category: {category}")
        if location:
            description_parts.append(f"Location: {location}")
        description = "\n".join(description_parts)

        vacancies.append(
            Vacancy(
                external_id=f"zalando:{job_id}",
                source_type=SourceType.career_site,
                title=title[:200] or "Position",
                company="Zalando",
                url=full_url,
                description=description,
                salary=None,
                location=location,
                published_at=None,
                raw={"site": "zalando", "category": category},
            )
        )
    return vacancies


def _parse_lesta(html: str, base_url: str) -> list[Vacancy]:
    """Парсер для join.lesta.team."""
    soup = BeautifulSoup(html, "html.parser")
    vacancies: list[Vacancy] = []
    seen_ids: set[str] = set()

    link_pattern = re.compile(r"^/vacancy/([a-z0-9\-]+)/(\d+)/?$")

    projects = ["Tanks Blitz", "Мир кораблей", "Мир танков", "General"]
    categories = [
        "Game Design", "Dev", "QA", "Art", "Animation&VFX", "BI",
        "Marketing", "Administration", "Infrastructure", "Management",
        "Support", "Video", "UI/UX", "General", "HR", "Legal",
    ]
    cities = ["Москва", "Минск", "Санкт-Петербург"]

    for link in soup.find_all("a", href=link_pattern):
        href = link.get("href", "")
        text = link.get_text(" ", strip=True)
        if not text:
            continue

        match = link_pattern.match(href)
        if not match:
            continue
        job_id = match.group(2)
        if job_id in seen_ids:
            continue
        seen_ids.add(job_id)

        # Текст вида: "Title Category project icon Project City м. Метро"
        category = next((c for c in categories if c in text), None)
        project = next((p for p in projects if p in text), None)
        city = next((c for c in cities if c in text), None)

        # Title — всё до первой найденной категории
        title = text
        if category and category in text:
            title = text.split(category)[0].strip()

        full_url = f"https://join.lesta.team{href}"

        description_parts = [title]
        if category:
            description_parts.append(f"Категория: {category}")
        if project:
            description_parts.append(f"Проект: {project}")
        if city:
            description_parts.append(f"Локация: {city}")
        description = "\n".join(description_parts)

        vacancies.append(
            Vacancy(
                external_id=f"lesta:{job_id}",
                source_type=SourceType.career_site,
                title=title[:200] or "Вакансия",
                company="Lesta Games",
                url=full_url,
                description=description,
                salary=None,
                location=city,
                published_at=None,
                raw={"site": "lesta", "category": category, "project": project},
            )
        )
    return vacancies
