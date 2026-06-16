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


def _registry() -> dict[str, tuple[str, str, ParserFn]]:
    return {
        "tochka": ("Точка Банк", "https://hr.tochka.com/vacancies/", _parse_tochka),
        "indrive": ("inDrive", "https://careers.indrive.com/vacancies/", _parse_indrive),
        "aviasales": ("Aviasales", "https://www.aviasales.ru/about/vacancies", _parse_aviasales),
        "garage_eight": ("Garage Eight", "https://garage-eight.com/vacancies/", _parse_garage_eight),
        "uzum": ("Uzum", "https://people.uzum.com/career/ru/vacancies", _parse_uzum),
        "avito": ("Avito", "https://career.avito.com/vacancies/", _parse_avito),
    }



# ---------- main source class ----------

class CareerSiteSource(JobSource):
    """Single class that dispatches to per-site parsers via registry.

    source.identifier holds the site_id ('tochka', 'indrive', ...).
    """

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    async def fetch(self, source: Source) -> list[Vacancy]:
        registry = _registry()
        entry = registry.get(source.identifier)
        if entry is None:
            return []
        _company, url, parser = entry

        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": USER_AGENT})
            if response.status_code != 200:
                return []
            html = response.text

        return parser(html, url)


def get_career_site_ids() -> list[str]:
    """All registered site_ids (used by provisioning)."""
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