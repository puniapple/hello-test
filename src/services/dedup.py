"""Deduplication helpers: content fingerprint and external link extraction."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse


# Job platform domains that uniquely identify a vacancy across sources
JOB_PLATFORM_DOMAINS = (
    "hh.ru",
    "headhunter.ru",
    "career.habr.com",
    "habr.com",
    "getmatch.ru",
    "getmatch.io",
    "boards.greenhouse.io",
    "jobs.lever.co",
    "apply.workable.com",
    "smartrecruiters.com",
    "ashbyhq.com",
    "wellfound.com",
    "linkedin.com/jobs",
    "pinpointhq.com",
    "telegra.ph",
)

URL_PATTERN = re.compile(
    r"https?://[\w\-.]+(?:/[^\s,;\)\]]*)?",
    re.IGNORECASE,
)


def normalize_for_fingerprint(text: str) -> str:
    """Lowercase + strip punctuation + collapse whitespace."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compute_content_fingerprint(
    title: str | None,
    company: str | None,
    description: str | None,
) -> str:
    """SHA256 of normalized title + company + first 200 chars of description.

    Catches reposts where the same vacancy is forwarded between channels
    with the exact (or near-exact) same wording.
    """
    parts = [
        normalize_for_fingerprint(title or ""),
        normalize_for_fingerprint(company or ""),
        normalize_for_fingerprint(description or "")[:200],
    ]
    key = "|".join(parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def extract_global_external_id(text: str) -> str | None:
    """Find a URL pointing to a known job platform.

    If the same vacancy is posted on multiple sources but all link to
    hh.ru/vacancy/12345 — that URL becomes the global ID.
    """
    if not text:
        return None

    for match in URL_PATTERN.finditer(text):
        url = match.group(0).rstrip(".,;)")
        try:
            parsed = urlparse(url)
        except Exception:
            continue
        host = (parsed.netloc or "").lower().lstrip("www.")
        path_combo = f"{host}{parsed.path}".lower()
        for domain in JOB_PLATFORM_DOMAINS:
            if domain in host or domain in path_combo:
                # Normalize: scheme + host + path (без query/fragment)
                return f"{parsed.scheme}://{host}{parsed.path}".rstrip("/")
    return None