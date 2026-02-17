from __future__ import annotations

import logging
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from siftr.config import AppConfig
from siftr.models import Compensation, JobPost, RemoteStatus
from siftr.seen_cache import SeenJobsCache
from siftr.util import ensure_dir, jitter_sleep, normalize_whitespace, parse_linkedin_relative_time, strip_location_from_company

log = logging.getLogger(__name__)

_POSTED_AGO_RE = re.compile(
    r"(reposted\s+)?\b\d+\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months)\s+ago\b",
    re.IGNORECASE,
)

# Browser tab title when viewing a job list (e.g. "(1) Top job picks for you") — do not use as job title.
_LIST_PAGE_TITLE_PATTERNS = (
    "top job picks",
    "recommended for you",
    "jobs you might like",
    "| linkedin",
    "linkedin jobs",
)


def _is_likely_list_page_title(title: str) -> bool:
    """True if this looks like the collection/list page tab title, not an individual job."""
    if not title or not title.strip():
        return True
    t = title.strip().lower()
    parts = [p.strip() for p in t.split("|") if p.strip()]
    # Job detail pages: "Role | Company | LinkedIn" (3 parts) or "Company hiring Role in Location | LinkedIn" (2 parts)
    if len(parts) >= 3 and parts[-1] == "linkedin":
        return False
    if len(parts) >= 2 and parts[-1] == "linkedin" and "hiring" in parts[0]:
        return False  # "Company hiring Role in Location | LinkedIn" — job detail, not list
    if any(p in t for p in _LIST_PAGE_TITLE_PATTERNS):
        return True
    # "(1) ..." or "(2) ..." style (number of unread / count)
    if re.match(r"^\s*\(\d+\)\s", t):
        return True
    return False


def _parse_current_job_id(url: str) -> str | None:
    try:
        qs = parse_qs(urlparse(url).query)
        if "currentJobId" in qs and qs["currentJobId"]:
            return qs["currentJobId"][0]
    except Exception:
        return None
    return None


def _parse_job_id_from_any(url: str) -> str | None:
    # supports /jobs/view/1234567890
    m = re.search(r"/jobs/view/(\d+)", url)
    if m:
        return m.group(1)
    return _parse_current_job_id(url)


def _url_with_start(url: str, start: int) -> str:
    """
    LinkedIn job collections/search support server-side pagination via `start=<offset>`.
    We prefer URL pagination over pure infinite scroll because collections often stop
    loading cards after a limited range.
    """
    start = max(0, int(start))
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=True)
    qs["start"] = [str(start)]
    new_query = urlencode(qs, doseq=True)
    return urlunparse(p._replace(query=new_query))


_RESULTS_COUNT_RE = re.compile(r"\b([\d,]+)\s+results\b", re.IGNORECASE)


def _extract_total_results_count(*, page) -> int | None:
    """
    Best-effort parse of the "X results" subtitle near the results list.
    Used only for progress/early stop hints; correctness must not depend on it.
    """

    def _parse(text: str) -> int | None:
        t = normalize_whitespace(text or "")
        m = _RESULTS_COUNT_RE.search(t)
        if not m:
            return None
        try:
            return int(m.group(1).replace(",", ""))
        except Exception:
            return None

    for sel in (
        ".jobs-search-results-list__subtitle",
        ".jobs-search-results-list__header",
        "header",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count():
                n = _parse(loc.inner_text(timeout=1_000))
                if n:
                    return n
        except Exception:
            continue

    try:
        hits = page.locator("text=/\\b[\\d,]+\\s+results\\b/i").all_inner_texts()
        for h in hits or []:
            n = _parse(h)
            if n:
                return n
    except Exception:
        pass

    return None


def _extract_list_job_ids(*, page) -> list[str]:
    """
    Extract job IDs from the left-side list without clicking into details.
    Prefer data attributes when present; fall back to parsing hrefs.
    """
    cards = page.locator("li.scaffold-layout__list-item, li.jobs-search-results__list-item")
    out: list[str] = []
    try:
        n = min(cards.count(), 2500)  # safety cap; LI can virtualize a lot
    except Exception:
        n = 0
    for i in range(n):
        card = cards.nth(i)
        job_id: str | None = None
        try:
            job_id = (card.get_attribute("data-occludable-job-id") or "").strip() or None
        except Exception:
            job_id = None
        if not job_id:
            try:
                href = card.locator("a").first.get_attribute("href")
                if href:
                    job_id = _parse_job_id_from_any(href)
            except Exception:
                job_id = None
        if job_id:
            out.append(str(job_id))
    return out


@dataclass(frozen=True)
class ScrapeResult:
    jobs: list[JobPost]
    cards_found: int
    extracted_count: int
    skipped_seen_cache_count: int


_CURRENCY_RE = r"(?:USD|US\$|\$)"
_DASH_RE = r"(?:-|–|—|to)"
# Amount tokens we commonly see in LinkedIn pay strings:
#   - 60,000.00
#   - 110K / 110k
#   - 1.2M
_AMOUNT_TOKEN_RE = r"(?:\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?:\s*[kKmM])?"
_AMOUNT_SUFFIX_RE = r"(?:\s*/\s*(?:yr|year|hr|hour)\b\.?|\s*per\s*(?:year|hour)\b|\s*(?:annually|hourly)\b)?"

_MONEY_RANGE_RE = re.compile(
    rf"(?P<currency>{_CURRENCY_RE})\s*(?P<a>{_AMOUNT_TOKEN_RE}){_AMOUNT_SUFFIX_RE}\s*{_DASH_RE}\s*(?:{_CURRENCY_RE})?\s*(?P<b>{_AMOUNT_TOKEN_RE})",
    re.IGNORECASE,
)

_MONEY_SINGLE_RE = re.compile(
    rf"(?P<currency>{_CURRENCY_RE})\s*(?P<a>{_AMOUNT_TOKEN_RE}){_AMOUNT_SUFFIX_RE}",
    re.IGNORECASE,
)


def _parse_amount_token(token: str) -> float | None:
    try:
        t = (token or "").strip()
        if not t:
            return None
        # normalize: remove commas/spaces
        t = t.replace(",", "").replace(" ", "")
        mult = 1.0
        if t[-1] in ("k", "K"):
            mult = 1_000.0
            t = t[:-1]
        elif t[-1] in ("m", "M"):
            mult = 1_000_000.0
            t = t[:-1]
        return float(t) * mult
    except Exception:
        return None


def _token_suffix_char(token: str) -> str | None:
    """
    Return a magnitude suffix (k/m) if present, else None.
    """
    t = (token or "").strip().replace(",", "").replace(" ", "")
    if not t:
        return None
    return t[-1] if t[-1] in ("k", "K", "m", "M") else None


def _apply_suffix_hint(token: str, suffix: str | None) -> str:
    """
    If one side of a range is written like "109.5k" and the other like "64.7",
    assume the suffix applies to both sides.
    """
    if not suffix:
        return token
    if _token_suffix_char(token):
        return token
    t = (token or "").strip()
    # Only apply hint when the number looks like a shorthand (< 1000),
    # to avoid incorrectly multiplying fully-qualified numbers like "64,700".
    try:
        t_clean = t.replace(",", "").replace(" ", "")
        v = float(t_clean)
        if v >= 1000.0:
            return token
    except Exception:
        return token
    return f"{t}{suffix}"


def _infer_interval(text: str) -> str:
    t = text or ""
    if re.search(r"/\s*yr\b|per\s*year\b|annual(?:ly)?\b|/\s*year\b|\byr\b", t, re.IGNORECASE):
        return "yearly"
    if re.search(r"/\s*hr\b|per\s*hour\b|hourly\b|/\s*hour\b|\bhr\b", t, re.IGNORECASE):
        return "hourly"
    return "unknown"


def _parse_pay_range(text: str) -> Compensation:
    """
    Best-effort parse for LinkedIn pay strings.

    LinkedIn variants observed (and commonly embedded in descriptions):
      - "Pay range: $90,000.00/yr - $140,000.00/yr"
      - "Base pay range $60,000.00/yr - $65,000.00/yr"
      - "Posted Salary Range USD $60,000.00 - $65,000.00 /Yr."
      - "$38/hr - $55/hr"
      - "$110K–$135K/yr"
    """
    t = normalize_whitespace(text)
    comp = Compensation(interval="unknown", currency="USD")
    if not t:
        return comp

    m = _MONEY_RANGE_RE.search(t)
    if m:
        a_raw = m.group("a")
        b_raw = m.group("b")
        # Suffix propagation: "64.7-109.5k" should be treated as "64.7k-109.5k".
        suffix_hint = _token_suffix_char(a_raw) or _token_suffix_char(b_raw)
        a = _parse_amount_token(_apply_suffix_hint(a_raw, suffix_hint))
        b = _parse_amount_token(_apply_suffix_hint(b_raw, suffix_hint))
        if a is None or b is None:
            return comp
        comp.min_amount = min(a, b)
        comp.max_amount = max(a, b)
        comp.interval = _infer_interval(t)
        # If LinkedIn omits the interval token ("/yr", "per hour", etc.), infer by magnitude.
        # Heuristic: <= $300 looks hourly; otherwise yearly.
        if comp.interval == "unknown":
            hi = comp.max_amount or comp.min_amount or 0
            comp.interval = "hourly" if float(hi) <= 300.0 else "yearly"
        return comp

    # Single-value fallback (better than treating as missing; interval may still be unknown).
    m = _MONEY_SINGLE_RE.search(t)
    if m:
        a = _parse_amount_token(m.group("a"))
        if a is None:
            return comp
        comp.min_amount = a
        comp.max_amount = None
        comp.interval = _infer_interval(t)
        if comp.interval == "unknown":
            comp.interval = "hourly" if float(comp.min_amount or 0) <= 300.0 else "yearly"
        return comp

    return comp


def _extract_compensation(*, page, description: str) -> Compensation:
    """
    Compensation extraction is brittle across LinkedIn shells/AB tests.
    Prefer to parse from the job description (often contains "Posted Salary Range ..."),
    then fall back to visible "Pay range"/"Base pay range" callouts.
    """
    # 1) Description is usually the richest source.
    comp = _parse_pay_range(description or "")
    if comp.min_amount is not None and comp.interval != "unknown":
        return comp

    # 2) Try commonly-used headings/callouts (signed-in and some logged-out shells).
    try:
        # IMPORTANT: the numeric range is often *not* in the same element as the heading text.
        # So we collect:
        # - the matching node's text
        # - its parent text
        # - and its grandparent text (usually contains the actual "$X - $Y" line)
        nodes = page.locator(
            "text=/Pay range|Base pay range|Posted Salary Range|Salary Range|Pay found in job post/i"
        )
        texts: list[str] = []
        try:
            texts.extend(nodes.all_inner_texts())
        except Exception:
            pass

        n = min(nodes.count(), 6)
        for i in range(n):
            try:
                el = nodes.nth(i)
                for xp in ("xpath=..", "xpath=../.."):
                    try:
                        t = el.locator(xp).inner_text(timeout=1_000)
                        if t:
                            texts.append(t)
                    except Exception:
                        continue
            except Exception:
                continue

        joined = " ".join([normalize_whitespace(x) for x in texts if x])
        comp2 = _parse_pay_range(joined)
        # If this yields a usable interval, prefer it.
        if comp2.min_amount is not None and comp2.interval != "unknown":
            return comp2
        # Otherwise, if we at least got a number, return it.
        if comp2.min_amount is not None:
            return comp2
    except Exception:
        pass

    # 3) If the description had a number but interval was unknown, return it (prefilter treats unknown as missing).
    if comp.min_amount is not None:
        return comp
    return Compensation()


def _remote_status_from_meta(meta_text: str) -> RemoteStatus:
    s = (meta_text or "").lower()
    if "remote" in s:
        # LinkedIn uses e.g. "Remote" / "Remote (United States)"
        return "remote"
    if "hybrid" in s:
        return "hybrid"
    if "on-site" in s or "onsite" in s or "on site" in s:
        return "onsite"
    return "unknown"


_LOCATION_IGNORE_RE = re.compile(
    r"\b(applicant|applicants|reposted|employee|employees|full[-\s]?time|part[-\s]?time|contract|intern(ship)?)\b",
    re.IGNORECASE,
)


def _pick_location_text(*, bullets: list[str], meta_text: str, company: str | None = None) -> str | None:
    """
    LinkedIn frequently reshuffles "top card" metadata. We try:
    - bullet chips (preferred)
    - meta_text chunks (fallback)
    """
    company_norm = (company or "").strip().lower()

    def _ok(s: str) -> bool:
        ss = normalize_whitespace(s)
        if not ss:
            return False
        if _POSTED_AGO_RE.search(ss or ""):
            return False
        if _LOCATION_IGNORE_RE.search(ss or ""):
            return False
        if company_norm and ss.strip().lower() == company_norm:
            return False
        return True

    cand = [normalize_whitespace(x) for x in (bullets or []) if _ok(x)]
    # Prefer explicit "Remote"/"Hybrid"/"On-site" strings.
    for s in cand:
        if re.search(r"\b(remote|hybrid|on[-\s]?site|onsite)\b", s, re.IGNORECASE):
            return s
    # Next, pick something that looks like a place.
    for s in cand:
        if ("," in s) or re.search(r"\b(United States|USA|Canada|UK|United Kingdom)\b", s, re.IGNORECASE):
            # If chunk is long or contains nav junk, extract location substring
            if len(s) > 80 or "skip to main content" in s.lower() or s.strip().lower().startswith("linkedin "):
                extracted = _extract_location_from_page_text(s)
                if extracted:
                    return extracted
            return s
    if cand:
        return cand[0]

    # Fallback: meta_text is often "<company> · <location> · <other...>"
    chunks: list[str] = []
    blob = normalize_whitespace(meta_text or "")
    if blob:
        for sep in ("·", "•", "\u00b7", "|"):
            if sep in blob:
                chunks = [normalize_whitespace(p) for p in blob.split(sep) if normalize_whitespace(p)]
                break
        if not chunks:
            chunks = [blob]
    chunks = [c for c in chunks if _ok(c)]
    for s in chunks:
        if re.search(r"\b(remote|hybrid|on[-\s]?site|onsite)\b", s, re.IGNORECASE):
            return s
    for s in chunks:
        if ("," in s) or re.search(r"\b(United States|USA|Canada|UK|United Kingdom)\b", s, re.IGNORECASE):
            # If chunk is long or contains nav junk (e.g. "Skip to main content"), extract location substring
            if len(s) > 80 or "skip to main content" in s.lower() or s.strip().lower().startswith("linkedin "):
                extracted = _extract_location_from_page_text(s)
                if extracted:
                    return extracted
            return s
    return chunks[0] if chunks else None


# Regexes for fallback location extraction when top-card DOM selectors fail (LinkedIn DOM changes).
_LOCATION_REMOTE_RE = re.compile(
    r"\b(Remote|Hybrid|On-site|On site|Onsite)(?:\s*\([^)]+\))?",
    re.IGNORECASE,
)
_LOCATION_CITY_ST_RE = re.compile(
    r"\b([A-Z][a-zA-Z\s\-]{1,35}),\s*([A-Z]{2})(?:\s+\d{5})?\b",
)
_LOCATION_IN_CITY_ST_RE = re.compile(
    r"\bin\s+([A-Z][a-zA-Z\s\-]{1,35}),\s*([A-Z]{2})(?:\s+\d{5})?\b",
)


def _extract_location_from_page_text(text: str) -> str | None:
    """
    Fallback when top-card selectors fail. Search page/description text for location patterns.
    Limits search to first ~4k chars to prefer header area over random mentions in body.
    """
    if not text or not text.strip():
        return None
    blob = text[:4000] if len(text) > 4000 else text
    blob = normalize_whitespace(blob)
    # Prefer Remote/Hybrid/On-site (often in header).
    m = _LOCATION_REMOTE_RE.search(blob)
    if m:
        return normalize_whitespace(m.group(0))
    # "in City, ST" — prefer this to avoid matching junk like "LinkedIn... in Mantua, OH"
    m = _LOCATION_IN_CITY_ST_RE.search(blob)
    if m:
        return f"{m.group(1).strip()}, {m.group(2)}"
    # Then "City, ST" — only use if city part is short (likely real location)
    m = _LOCATION_CITY_ST_RE.search(blob)
    if m:
        city = m.group(1).strip()
        if len(city) <= 40:
            return f"{city}, {m.group(2)}"
    return None


def _extract_company_logo_url(*, top) -> str | None:
    """
    Best-effort: extract the posting company's logo image URL from the top card.
    LinkedIn often uses `src` or `data-delayed-url`/`data-delayed-url` for lazy loading.
    """
    selectors = [
        # common containers
        ".jobs-unified-top-card__company-logo img",
        ".job-details-jobs-unified-top-card__company-logo img",
        "img.jobs-unified-top-card__company-logo",
        "img.job-details-jobs-unified-top-card__company-logo",
        # sometimes just "company logo" image within top card
        "img[alt*='logo' i]",
    ]
    for sel in selectors:
        try:
            img = top.locator(sel).first
            if not img.count():
                continue
            src = (img.get_attribute("src") or "").strip()
            if not src:
                src = (img.get_attribute("data-delayed-url") or "").strip()
            if not src:
                src = (img.get_attribute("data-ghost-url") or "").strip()
            if not src:
                # last resort: parse srcset
                srcset = (img.get_attribute("srcset") or "").strip()
                if srcset:
                    # take the first URL token
                    src = (srcset.split(",")[0].strip().split(" ")[0] or "").strip()
            if src:
                return src
        except Exception:
            continue
    return None


def _is_login_or_checkpoint_url(url: str) -> bool:
    u = (url or "").lower()
    return (
        ("linkedin.com/login" in u)
        or ("linkedin.com/uas/login" in u)
        or ("linkedin.com/checkpoint" in u)
    )


# Page title formats observed:
# - "Role | Company | LinkedIn" (signed-in, some layouts)
# - "Company hiring Role in Location | LinkedIn" (guest/public view)
_TITLE_COMPANY_HIRING_RE = re.compile(
    r"^(.+?)\s+hiring\s+",
    re.IGNORECASE,
)
_TITLE_ROLE_IN_LOCATION_RE = re.compile(
    r"\bhiring\s+(.+?)\s+in\s+",
    re.IGNORECASE,
)


def _extract_title_from_page_title(page_title: str) -> str | None:
    """
    Extract job title from LinkedIn page title when DOM selectors fail.
    Supports: "Role | Company | LinkedIn" (3 parts) and "Company hiring Role in Location | LinkedIn" (2 parts).
    """
    t = (page_title or "").strip()
    if not t:
        return None
    parts = [p.strip() for p in t.split("|") if p.strip()]
    # Format: "Role | Company | LinkedIn" — first part is the role
    if len(parts) >= 3 and parts[-1].lower() == "linkedin":
        return normalize_whitespace(parts[0])
    # Format: "Company hiring Role in Location | LinkedIn" — extract role between "hiring " and " in "
    if len(parts) >= 2 and parts[-1].lower() == "linkedin":
        first = parts[0]
        m = _TITLE_ROLE_IN_LOCATION_RE.search(first)
        if m:
            return normalize_whitespace(m.group(1))
        # Fallback: "Company hiring Role" (no " in Location") — take everything after "hiring "
        if re.search(r"\bhiring\s+", first, re.I):
            after = re.split(r"\bhiring\s+", first, maxsplit=1, flags=re.I)[-1]
            return normalize_whitespace(after) if after else None
    return None


def _extract_company_from_title(title: str) -> str | None:
    """
    Extract company name from LinkedIn page title when DOM selectors fail.
    Supports: "Role | Company | LinkedIn" and "Company hiring Role in Location | LinkedIn".
    """
    t = (title or "").strip()
    if not t:
        return None
    parts = [p.strip() for p in t.split("|") if p.strip()]
    # Format: "Role | Company | LinkedIn"
    if len(parts) >= 3 and parts[-1].lower() == "linkedin":
        return parts[1]
    # Format: "Company hiring Role in Location | LinkedIn" — first part before "|"
    if len(parts) >= 2 and parts[-1].lower() == "linkedin":
        first = parts[0]
        m = _TITLE_COMPANY_HIRING_RE.match(first)
        if m:
            return normalize_whitespace(m.group(1))
    return None


# Job URL slug: /jobs/view/role-at-companyname-1234567890 or /jobs/view/1234567890
_URL_AT_COMPANY_RE = re.compile(
    r"-at-([a-z0-9]+(?:-[a-z0-9]+)*)-\d+$",
    re.IGNORECASE,
)


def _extract_company_from_job_url(url: str) -> str | None:
    """
    Extract company name from LinkedIn job URL slug when present.
    E.g. .../jobs/view/technical-support-engineer-at-remotehunter-4372308994 -> "RemoteHunter"
    """
    u = (url or "").strip()
    if not u or "/jobs/view/" not in u:
        return None
    # Take path after /jobs/view/
    try:
        path = urlparse(u).path
        if "/jobs/view/" in path:
            slug = path.split("/jobs/view/")[-1].split("?")[0]
            m = _URL_AT_COMPANY_RE.search(slug)
            if m:
                # "remotehunter" -> "RemoteHunter" (title case)
                raw = m.group(1)
                return raw.replace("-", " ").title()
    except Exception:
        pass
    return None


# Location patterns for "company between title and location" heuristic.
# Order matters: more specific patterns first.
_LOCATION_PATTERNS_FOR_COMPANY_EXTRACT = [
    re.compile(r"\bRemote\s*\([^)]+\)", re.IGNORECASE),  # Remote (United States)
    re.compile(r"\bRemote\b", re.IGNORECASE),
    re.compile(r"\bHybrid\b", re.IGNORECASE),
    re.compile(r"\bOn-site\b", re.IGNORECASE),
    re.compile(r"\bOn\s+site\b", re.IGNORECASE),
    re.compile(r"\bOnsite\b", re.IGNORECASE),
    re.compile(r"\bUnited\s+States\b", re.IGNORECASE),
    re.compile(r"\bUSA\b", re.IGNORECASE),
    re.compile(r"\bU\.?S\.?A\.?\b", re.IGNORECASE),
    re.compile(r"\bCanada\b", re.IGNORECASE),
    re.compile(r"\bUK\b", re.IGNORECASE),
    re.compile(r"\bUnited\s+Kingdom\b", re.IGNORECASE),
    # City, ST - require exactly 2 words (e.g. San Francisco, New York) to avoid matching "Inc San Francisco"
    re.compile(r"\b[A-Z][a-z]+\s+[A-Z][a-z]+,\s*[A-Z]{2}\b"),
]


def _extract_company_between_title_and_location(header_text: str, title: str) -> str | None:
    """
    Extract company name from the text between the role title and the location.
    LinkedIn job cards consistently use: Title | Company | Location.
    """
    if not header_text or not (title or "").strip():
        return None
    text = normalize_whitespace(header_text)
    title_clean = normalize_whitespace(title)
    if not title_clean or title_clean not in text:
        return None

    idx = text.find(title_clean)
    after_title = text[idx + len(title_clean) :]

    for pat in _LOCATION_PATTERNS_FOR_COMPANY_EXTRACT:
        m = pat.search(after_title)
        if m:
            company = after_title[: m.start()].strip()
            company = normalize_whitespace(company).strip("·•|\t\n\r -")
            if not company or len(company) > 100:
                continue
            if re.search(r"\d+\s+(hour|day|week|month)s?\s+ago", company, re.I):
                continue
            if "applicant" in company.lower():
                continue
            if (company or "").strip().lower() in ("linkedin", "n/a"):
                continue
            return company.strip()
    return None


def _extract_company_from_canvas_first_100_words(
    canvas_text: str,
    blocklist_exact: list[str],
) -> str | None:
    """
    When DOM parsing fails, check if any blocklist term appears in the first 100 words
    of the job canvas (top card + description). Used only when company is N/A to avoid
    false positives from "Similar jobs" sections further down the page.
    """
    if not canvas_text or not blocklist_exact:
        return None
    words = normalize_whitespace(canvas_text).split()[:100]
    first_100 = " ".join(words).lower()
    for term in blocklist_exact:
        t = (term or "").strip()
        if not t:
            continue
        if re.search(r"\b" + re.escape(t.lower()) + r"\b", first_100):
            return t  # preserve original casing from blocklist
    return None


# Patterns to extract company from job description text (first ~300 chars).
# Order: more specific first.
_DESCRIPTION_COMPANY_PATTERNS = [
    re.compile(r"^About\s+([A-Za-z0-9][A-Za-z0-9\s&\-.,'()]+?)(?:\s+[–—-]\s|\.\s|$)", re.MULTILINE),
    re.compile(r"^([A-Za-z0-9][A-Za-z0-9\s&\-.,'()]+?)\s+is\s+(?:hiring|looking|seeking)", re.IGNORECASE),
    re.compile(r"\bAt\s+([A-Za-z0-9][A-Za-z0-9\s&\-.,'()]{2,50}?)(?:\s*,\s|\s+[–—-]\s|\.\s|$)", re.MULTILINE),
    re.compile(r"\bat\s+([A-Za-z0-9][A-Za-z0-9\s&\-.,'()]{2,50}?)(?:\s+[–—-]\s|\.\s|,|\s+is\s|$)", re.IGNORECASE),
]


def _extract_company_from_description(canvas_text: str) -> str | None:
    """
    Extract company name from job description intro (e.g. "About Company Name –", "Company is hiring").
    Scoped to first ~300 chars to avoid false positives from "Similar jobs" or body text.
    """
    if not canvas_text or len(canvas_text) < 10:
        return None
    intro = normalize_whitespace(canvas_text)[:300]
    for pat in _DESCRIPTION_COMPANY_PATTERNS:
        m = pat.search(intro)
        if m:
            raw = m.group(1).strip()
            if len(raw) < 3 or len(raw) > 80:
                continue
            if re.search(r"\d+\s+(hour|day|week|month)s?\s+ago", raw, re.I):
                continue
            if raw.lower() in ("linkedin", "n/a", "the", "we", "our", "this"):
                continue
            return raw
    return None


def _extract_company_from_list_card(*, card, title: str) -> str | None:
    """
    Extract company name from the left-side list card (before or after clicking).
    LinkedIn list cards often show: Job Title | Company | Location. The company may appear
    as a link to /company/ or as secondary text. This is especially useful when the detail
    panel DOM selectors fail (e.g. LinkedIn UI changes).
    """
    try:
        # Strategy 1: Company link (a[href*='/company/']) - most reliable when present
        company_link = card.locator("a[href*='/company/']").first
        if company_link.count():
            txt = normalize_whitespace(company_link.inner_text(timeout=1_000))
            if txt and len(txt) <= 80 and txt.lower() not in ("linkedin", "n/a"):
                return txt.strip()
    except Exception:
        pass

    try:
        # Strategy 2: Parse card text - format is often "Title" then "Company" then location
        card_text = normalize_whitespace(card.inner_text(timeout=1_500))
        if card_text and title:
            extracted = _extract_company_between_title_and_location(card_text[:500], title)
            if extracted:
                return extracted
    except Exception:
        pass

    return None


def _extract_date_posted(*, page) -> tuple[datetime | None, str | None]:
    """
    Extract the relative posted time from the top-card area and parse to datetime.
    Returns (date_posted_dt, date_posted_text).
    """
    try:
        top = page.locator(
            ".job-details-jobs-unified-top-card__container, .jobs-unified-top-card, .jobs-details-top-card"
        ).first

        # Try to find "X ago" (or "Reposted X ago") within the top card.
        candidates = top.locator("text=/\\b(reposted\\s+)?\\d+\\s+(minute|hour|day|week|month)s?\\s+ago\\b/i").all_inner_texts()
        blob = " ".join([normalize_whitespace(c) for c in candidates if c]) if candidates else ""
        if not blob:
            # fallback: look at primary description container (often includes bullets)
            blob = normalize_whitespace(
                top.locator(
                    ".jobs-unified-top-card__primary-description, .job-details-jobs-unified-top-card__primary-description, .job-details-jobs-unified-top-card__primary-description-container"
                ).first.inner_text(timeout=1_000)
            )

        # Pick the first matching "X ago" phrase.
        m = _POSTED_AGO_RE.search(blob or "")
        if m:
            text = m.group(0)
            return parse_linkedin_relative_time(text), text

        return None, None
    except Exception:
        return None, None


def _detect_already_applied(*, page, card) -> tuple[bool | None, str | None]:
    """
    Best-effort detection of the LinkedIn "Applied" state.

    Signals we look for (in order):
    - A top-card link/button containing "See application"
    - A top-card text snippet containing "Applied" (e.g. "Applied 1 day ago")
    - A job-card list label containing "Applied"
    """
    try:
        top = page.locator(
            ".job-details-jobs-unified-top-card__container, .jobs-unified-top-card, .jobs-details-top-card"
        ).first

        see_app = top.locator("a:has-text('See application'), button:has-text('See application')").first
        if see_app.count():
            try:
                return True, normalize_whitespace(see_app.inner_text(timeout=1_000))
            except Exception:
                return True, "See application"

        applied = top.locator("text=/\\bApplied\\b/i").first
        if applied.count():
            try:
                txt = normalize_whitespace(applied.inner_text(timeout=1_000))
            except Exception:
                txt = "Applied"
            # guard against false positives: if the snippet is huge, it's probably from description
            if txt and len(txt) <= 120:
                return True, txt
            return True, "Applied"

        # fallback: check the left-side card itself
        card_applied = card.locator("text=/\\bApplied\\b/i").first
        if card_applied.count():
            try:
                txt = normalize_whitespace(card_applied.inner_text(timeout=500))
            except Exception:
                txt = "Applied"
            if txt and len(txt) <= 120:
                return True, txt
            return True, "Applied"

        return False, None
    except Exception:
        return None, None


def scrape_top_applicant(
    cfg: AppConfig,
    *,
    login: bool = False,
    collection_url: str | None = None,
    collection_name: str | None = None,
    seen_cache: SeenJobsCache | None = None,
) -> ScrapeResult:
    out_dir = ensure_dir(Path(cfg.run.out_dir))
    ensure_dir(out_dir / "debug")
    ensure_dir(out_dir / "profiles")

    user_data_dir = str(Path(cfg.scrape.browser.user_data_dir))
    target_url = (collection_url or cfg.run.top_applicant_url or "").strip()
    if not target_url:
        raise ValueError("Missing collection URL to scrape.")

    with sync_playwright() as p:
        browser_type = p.chromium
        log.info(
            "Launching browser (headless=%s, slow_mo_ms=%s, user_data_dir=%s)",
            bool(cfg.scrape.browser.headless) if not login else False,
            int(cfg.scrape.browser.slow_mo_ms or 0),
            user_data_dir,
        )
        context = browser_type.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False if login else bool(cfg.scrape.browser.headless),
            slow_mo=int(cfg.scrape.browser.slow_mo_ms or 0),
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        # LinkedIn can redirect to /checkpoint *after* the initial goto returns. In login
        # mode, we want to allow the user time to complete MFA/challenges/passkeys.
        overall_timeout_s = 600.0 if login else 45.0
        deadline = time.monotonic() + overall_timeout_s

        # Core UI: left list + right detail panel.
        #
        # IMPORTANT: LinkedIn frequently renders the results list inside a
        # `div.scaffold-layout__list` which *contains* a plain `<ul class="...">`
        # with non-stable classnames. Prefer stable container and stable list items.
        cards_locator = page.locator(
            ",".join(
                [
                    "li.scaffold-layout__list-item[data-occludable-job-id]",
                    "li.jobs-search-results__list-item",
                ]
            )
        )

        def _pick_scroll_container():
            for sel in [
                "div.scaffold-layout__list",
                "ul.scaffold-layout__list-container",
                "ul.jobs-search-results__list",
                "div.jobs-search-results-list",
                "main.scaffold-layout__list-detail",
            ]:
                loc = page.locator(sel).first
                try:
                    if loc.count():
                        return loc
                except Exception:
                    continue
            return page.locator("body").first

        def _ensure_cards_loaded(*, nav_url: str) -> None:
            log.info("Navigating to collection page: %s", nav_url)
            page.goto(nav_url, wait_until="domcontentloaded")
            log.debug("Current URL after goto: %s", page.url)

            while True:
                # If redirected to login/checkpoint, either prompt for manual auth (login mode)
                # or fail fast (non-login mode).
                if _is_login_or_checkpoint_url(page.url):
                    log.info("LinkedIn redirected to %s", page.url)
                    if not login:
                        context.close()
                        raise RuntimeError(
                            "Not logged in (LinkedIn redirected to a login/checkpoint page). "
                            "Re-run with `--login` to complete LinkedIn authentication."
                        )

                    input(
                        "LinkedIn needs authentication (checkpoint/MFA). Complete it in the browser, "
                        "then press Enter here to continue..."
                    )
                    try:
                        page.goto(nav_url, wait_until="domcontentloaded")
                    except PlaywrightError as e:
                        # LinkedIn may redirect to a checkpoint/challenge during goto; treat as "still loading".
                        if "interrupted" in str(e).lower() or "navigation" in str(e).lower():
                            log.info("Navigation was redirected (e.g. checkpoint); continuing. %s", e)
                        else:
                            raise
                    log.debug("Current URL after post-login goto: %s", page.url)

                try:
                    # Wait for actual job cards (more reliable than container tags).
                    cards_locator.first.wait_for(state="visible", timeout=5_000)
                    return
                except PlaywrightTimeoutError:
                    if time.monotonic() >= deadline:
                        # Save for debugging
                        try:
                            page.screenshot(
                                path=str(out_dir / "debug" / "top_applicant_load_failed.png"),
                                full_page=True,
                            )
                        except Exception:
                            pass
                        try:
                            (out_dir / "debug" / "top_applicant_load_failed.html").write_text(
                                page.content(), encoding="utf-8"
                            )
                        except Exception:
                            pass
                        url = page.url
                        context.close()
                        raise RuntimeError(
                            "Could not find any job cards before timeout. "
                            f"Current URL: {url!r}. LinkedIn DOM may have changed or you may still be on a checkpoint page."
                        )
                    # loop again: could be slow load, redirect, or DOM drift
                    continue

        page_size = 24  # LinkedIn commonly paginates in 24-result chunks
        max_total_jobs = max(1, int(cfg.scrape.max_jobs or 1))

        jobs: list[JobPost] = []
        seen: set[str] = set()
        skipped_seen_cache = 0
        cards_found_total = 0

        total_results_hint: int | None = None
        pages_without_new = 0
        seen_list_ids: set[str] = set()
        last_page_signature: tuple[str | None, str | None, int] | None = None

        def _extract_jobs_from_current_loaded_page(*, remaining: int) -> tuple[list[JobPost], int, int, list[str]]:
            """
            Extract as many jobs as we can from the currently loaded collection page.
            Returns (jobs, cards_found, skipped_seen_cache_delta, list_job_ids).
            """
            list_locator = _pick_scroll_container()

            def current_cards_count() -> int:
                return page.locator(
                    "li.jobs-search-results__list-item, li.scaffold-layout__list-item"
                ).count()

            # Infinite scroll within this `start=` page.
            stable_rounds = 0
            while current_cards_count() < cfg.scrape.max_jobs and stable_rounds < 5:
                before = current_cards_count()
                list_locator = _pick_scroll_container()
                try:
                    list_locator.evaluate("el => { el.scrollTop = el.scrollHeight; }")
                except Exception:
                    pass
                jitter_sleep(cfg.scrape.rate_limit.min_delay_seconds, cfg.scrape.rate_limit.max_delay_seconds)
                after = current_cards_count()
                stable_rounds = stable_rounds + 1 if after <= before else 0
                log.debug("Scroll round: %s -> %s cards (stable_rounds=%s)", before, after, stable_rounds)

            list_job_ids = _extract_list_job_ids(page=page)
            cards = page.locator("li.jobs-search-results__list-item, li.scaffold-layout__list-item")
            cards_found = cards.count()
            log.info("Found %s job cards on page", cards_found)

            out: list[JobPost] = []
            skipped_delta = 0

            # Iterate over all cards, but stop once we've filled `remaining`.
            for i in range(cards_found):
                if len(out) >= remaining:
                    break

                card = cards.nth(i)
                if i and i % 10 == 0:
                    log.info("Extracting job %s / %s on this page", i + 1, cards_found)

                # Best-effort: parse job_id from the card link before clicking.
                pre_href: str | None = None
                pre_job_id: str | None = None
                try:
                    pre_href = card.locator("a").first.get_attribute("href")
                    if pre_href:
                        pre_job_id = _parse_job_id_from_any(pre_href)
                except Exception:
                    pre_href = None
                    pre_job_id = None

                if pre_job_id and pre_job_id in seen:
                    continue

                if pre_job_id and seen_cache and seen_cache.should_skip_scan(pre_job_id):
                    seen_cache.mark_seen(
                        job_id=pre_job_id,
                        job_url=pre_href,
                        collection_name=collection_name,
                    )
                    skipped_delta += 1
                    continue

                try:
                    card.scroll_into_view_if_needed(timeout=10_000)
                except Exception:
                    pass

                # click the card (sometimes nested button/a)
                try:
                    card.click(timeout=5_000)
                except Exception:
                    try:
                        card.locator("a").first.click(timeout=5_000)
                    except Exception:
                        continue

                jitter_sleep(cfg.scrape.rate_limit.min_delay_seconds, cfg.scrape.rate_limit.max_delay_seconds)

                # Wait for the right-side detail panel to attach content.
                try:
                    page.locator(
                        ",".join(
                            [
                                ".job-details-jobs-unified-top-card__container",
                                ".jobs-unified-top-card",
                                ".jobs-details-top-card",
                                "[componentkey^='JobDetails_']",
                            ]
                        )
                    ).first.wait_for(state="attached", timeout=5_000)
                except Exception:
                    pass

                job_id = _parse_job_id_from_any(page.url)
                if not job_id:
                    try:
                        href = card.locator("a").first.get_attribute("href")
                        if href:
                            job_id = _parse_job_id_from_any(href)
                    except Exception:
                        job_id = None

                if not job_id or job_id in seen:
                    continue

                seen.add(job_id)
                log.debug("Selected job_id=%s url=%s", job_id, page.url)

                if seen_cache:
                    seen_cache.mark_seen(
                        job_id=job_id,
                        job_url=page.url,
                        collection_name=collection_name,
                    )

                    # Safety net: if we couldn't parse job_id from the card href pre-click (DOM drift),
                    # bail out before doing expensive detail scraping.
                    if seen_cache.should_skip_scan(job_id):
                        skipped_delta += 1
                        continue

                already_applied, applied_text = _detect_already_applied(page=page, card=card)
                if already_applied is True:
                    log.debug("Detected already applied for job_id=%s (%s)", job_id, applied_text)

                top = page.locator(
                    ".job-details-jobs-unified-top-card__container, .jobs-unified-top-card, .jobs-details-top-card"
                ).first

                # Title/company/location in top card
                title = ""
                for sel in (
                    ".job-details-jobs-unified-top-card__job-title",
                    ".jobs-unified-top-card__job-title",
                    "h1",
                    "h2",
                ):
                    try:
                        el = top.locator(sel).first
                        if el.count():
                            title = normalize_whitespace(el.inner_text(timeout=3_000))
                            if title:
                                break
                    except Exception:
                        continue
                if not title:
                    # Fallback: list card often has the job title in the link text (avoids list page tab title).
                    try:
                        link_el = card.locator("a[href*='/jobs/']").first
                        if link_el.count():
                            title = normalize_whitespace(link_el.inner_text(timeout=2_000))
                        if not title:
                            link_el = card.locator("a").first
                            if link_el.count():
                                title = normalize_whitespace(link_el.inner_text(timeout=2_000))
                    except Exception:
                        pass
                if not title:
                    try:
                        t = (page.title() or "").strip()
                        if t and not _is_likely_list_page_title(t):
                            extracted = _extract_title_from_page_title(t)
                            if extracted:
                                title = extracted
                    except Exception:
                        title = ""
                if not title:
                    title = "N/A"

                company = "N/A"
                company_url: str | None = None
                try:
                    company_el = top.locator(
                        ",".join(
                            [
                                "a.jobs-unified-top-card__company-name",
                                "a.jobs-unified-top-card__company-name-link",
                                "a[data-control-name='company_link']",
                                "span.jobs-unified-top-card__company-name",
                                "span.job-details-jobs-unified-top-card__company-name",
                                ".job-details-jobs-unified-top-card__company-name",
                                ".jobs-unified-top-card__company-name",
                                "a[href*='/company/']",
                                # Broader selectors for LinkedIn UI drift
                                "[data-tracking-control-name='public_jobs_topcard-org-name']",
                                ".job-details-top-card__company-url",
                            ]
                        )
                    ).first
                    if company_el.count():
                        company = normalize_whitespace(company_el.inner_text(timeout=3_000))
                        company_url = company_el.get_attribute("href")
                except Exception:
                    pass
                if company == "N/A":
                    # Fallback 0: left-side list card (company often visible there)
                    try:
                        extracted = _extract_company_from_list_card(card=card, title=title)
                        if extracted:
                            company = extracted
                    except Exception:
                        pass
                if company == "N/A":
                    # Fallback 1: text between title and location (consistent card format)
                    try:
                        header_text = page.locator("body").inner_text(timeout=2_000)
                        header_text = (header_text or "")[:800]
                        extracted = _extract_company_between_title_and_location(header_text, title)
                        if extracted:
                            company = extracted
                    except Exception:
                        pass
                if company == "N/A":
                    # Fallback 2: page title ("Role | Company | LinkedIn" or "Company hiring Role...")
                    try:
                        t = (page.title() or "").strip()
                        extracted = _extract_company_from_title(t)
                        if extracted:
                            company = extracted
                    except Exception:
                        pass
                if company == "N/A":
                    # Fallback 3: URL slug (e.g. .../technical-support-engineer-at-remotehunter-4372308994)
                    try:
                        extracted = _extract_company_from_job_url(page.url)
                        if extracted:
                            company = extracted
                    except Exception:
                        pass
                if company == "N/A":
                    # Fallback 4: first 100 words of job canvas (top card + description)
                    # Scoped to avoid false positives from "Similar jobs" further down the page.
                    try:
                        canvas_parts: list[str] = []
                        canvas_parts.append(top.inner_text(timeout=2_000))
                        desc_loc = page.locator(
                            "div.jobs-description__content, div.jobs-description-content__text, div.jobs-box__html-content"
                        ).first
                        if desc_loc.count():
                            canvas_parts.append(desc_loc.inner_text(timeout=3_000))
                        canvas_text = normalize_whitespace(" ".join(canvas_parts))
                        blocklist = getattr(cfg.filters.company_blocklist, "exact", None) or []
                        extracted = _extract_company_from_canvas_first_100_words(canvas_text, blocklist)
                        if extracted:
                            company = extracted
                    except Exception:
                        pass
                if company == "N/A":
                    # Fallback 5: parse description intro ("About Company –", "Company is hiring")
                    try:
                        canvas_parts_5: list[str] = []
                        canvas_parts_5.append(top.inner_text(timeout=2_000))
                        desc_loc_5 = page.locator(
                            "div.jobs-description__content, div.jobs-description-content__text, div.jobs-box__html-content"
                        ).first
                        if desc_loc_5.count():
                            canvas_parts_5.append(desc_loc_5.inner_text(timeout=3_000))
                        canvas_text_5 = normalize_whitespace(" ".join(canvas_parts_5))
                        extracted = _extract_company_from_description(canvas_text_5)
                        if extracted:
                            company = extracted
                    except Exception:
                        pass
                if (company or "").strip().lower() == "linkedin" and not company_url:
                    company = "N/A"

                company_logo_url: str | None = None
                try:
                    company_logo_url = _extract_company_logo_url(top=top)
                except Exception:
                    company_logo_url = None

                meta_text = ""
                try:
                    meta_text = normalize_whitespace(
                        top.locator(
                            ".jobs-unified-top-card__primary-description, .job-details-jobs-unified-top-card__primary-description"
                        ).first.inner_text(timeout=3_000)
                    )
                except Exception:
                    meta_text = ""

                if not meta_text:
                    try:
                        bullets = top.locator(
                            ".jobs-unified-top-card__bullet, .job-details-jobs-unified-top-card__bullet"
                        ).all_inner_texts()
                        meta_text = normalize_whitespace(
                            " ".join([normalize_whitespace(x) for x in (bullets or []) if x])
                        )
                    except Exception:
                        meta_text = ""

                remote_status = _remote_status_from_meta(meta_text)

                bullets2: list[str] = []
                try:
                    bullets2 = top.locator(
                        ".jobs-unified-top-card__bullet, .job-details-jobs-unified-top-card__bullet"
                    ).all_inner_texts()
                except Exception:
                    bullets2 = []
                location_text = _pick_location_text(bullets=bullets2, meta_text=meta_text, company=company)

                date_posted, date_posted_text = _extract_date_posted(page=page)
                if not date_posted_text:
                    try:
                        body_text = normalize_whitespace(page.locator("body").inner_text(timeout=2_000))
                        m = _POSTED_AGO_RE.search(body_text or "")
                        if m:
                            date_posted_text = m.group(0)
                            date_posted = parse_linkedin_relative_time(date_posted_text)
                    except Exception:
                        pass

                description = ""
                try:
                    desc_el = page.locator(
                        "div.jobs-description__content, div.jobs-description-content__text, div.jobs-box__html-content"
                    ).first
                    if desc_el.count():
                        description = desc_el.inner_text(timeout=10_000)
                except Exception:
                    description = ""

                description = (description or "").strip()
                if not description:
                    html = page.content()
                    soup = BeautifulSoup(html, "html.parser")
                    for bad in soup(["script", "style", "noscript"]):
                        bad.decompose()
                    description = soup.get_text(" ", strip=True)

                if not location_text and description:
                    location_text = _extract_location_from_page_text(description)
                if not location_text:
                    try:
                        body_text = normalize_whitespace(page.locator("body").inner_text(timeout=2_000))
                        location_text = _extract_location_from_page_text(body_text or "")
                    except Exception:
                        pass

                comp = _extract_compensation(page=page, description=description)

                job_url = f"https://www.linkedin.com/jobs/view/{job_id}"

                if seen_cache:
                    seen_cache.mark_seen(
                        job_id=str(job_id),
                        job_url=job_url,
                        collection_name=collection_name,
                    )
                    seen_cache.mark_scanned(
                        job_id=str(job_id),
                        title=title or None,
                        company=company or None,
                        now=datetime.now(timezone.utc),
                    )

                company_clean = (
                    strip_location_from_company(company)
                    if (company and str(company).strip() != "N/A")
                    else (company or "N/A")
                )
                out.append(
                    JobPost(
                        job_id=str(job_id),
                        job_url=job_url,
                        title=title or "N/A",
                        company=company_clean or "N/A",
                        company_url=company_url,
                        company_logo_url=company_logo_url,
                        location_text=location_text,
                        remote_status=remote_status,
                        date_posted=date_posted,
                        date_posted_text=date_posted_text,
                        compensation=comp,
                        description=description,
                        already_applied=already_applied,
                        applied_text=applied_text,
                    )
                )

            return out, cards_found, skipped_delta, list_job_ids

        start = 0
        while len(jobs) < max_total_jobs and pages_without_new < 2:
            nav_url = _url_with_start(target_url, start)
            _ensure_cards_loaded(nav_url=nav_url)
            log.info("Job cards visible; collection loaded (start=%s)", start)

            if total_results_hint is None:
                try:
                    total_results_hint = _extract_total_results_count(page=page)
                    if total_results_hint:
                        log.info("Collection reports ~%s results", total_results_hint)
                except Exception:
                    total_results_hint = None

            if total_results_hint is not None and start >= total_results_hint:
                log.info("Reached end of collection (start=%s >= results=%s)", start, total_results_hint)
                break

            remaining = max_total_jobs - len(jobs)
            page_jobs, cards_found, skipped_delta, list_ids = _extract_jobs_from_current_loaded_page(remaining=remaining)
            cards_found_total += cards_found
            skipped_seen_cache += skipped_delta
            jobs.extend(page_jobs)

            sig = (list_ids[0], list_ids[-1], len(list_ids)) if list_ids else (None, None, 0)
            if last_page_signature and sig == last_page_signature and start > 0:
                log.warning("Pagination appears stuck (same card signature across pages). Stopping.")
                break
            last_page_signature = sig

            new_list_ids = [x for x in list_ids if x not in seen_list_ids]
            if not new_list_ids:
                pages_without_new += 1
                log.info("No new list job IDs found on this page (pages_without_new=%s)", pages_without_new)
            else:
                pages_without_new = 0
                seen_list_ids.update(new_list_ids)

            if list_ids and len(list_ids) < page_size:
                log.info("Last page detected (only %s cards in list)", len(list_ids))
                break

            start += page_size

        context.close()
        if skipped_seen_cache:
            log.info(
                "Seen-cache skipped %s job(s) (scanned within %s day(s))",
                skipped_seen_cache,
                int(getattr(cfg.scrape.seen_cache, "skip_if_scanned_within_days", 0)),
            )
        log.info("Scrape complete (%s unique jobs)", len(jobs))
        return ScrapeResult(
            jobs=jobs,
            cards_found=cards_found_total,
            extracted_count=len(jobs),
            skipped_seen_cache_count=skipped_seen_cache,
        )


def scrape_job_url(
    cfg: AppConfig,
    *,
    job_url: str,
    login: bool = False,
    seen_cache: SeenJobsCache | None = None,
) -> JobPost:
    """
    Scrape a single LinkedIn job posting URL and return a JobPost.

    This is intended for prompt iteration / evaluator tuning.
    """
    out_dir = ensure_dir(Path(cfg.run.out_dir))
    ensure_dir(out_dir / "debug")
    ensure_dir(out_dir / "profiles")

    user_data_dir = str(Path(cfg.scrape.browser.user_data_dir))

    with sync_playwright() as p:
        browser_type = p.chromium
        log.info(
            "Launching browser (headless=%s, slow_mo_ms=%s, user_data_dir=%s)",
            bool(cfg.scrape.browser.headless) if not login else False,
            int(cfg.scrape.browser.slow_mo_ms or 0),
            user_data_dir,
        )
        context = browser_type.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False if login else bool(cfg.scrape.browser.headless),
            slow_mo=int(cfg.scrape.browser.slow_mo_ms or 0),
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        log.info("Navigating to job URL")
        page.goto(job_url, wait_until="domcontentloaded")
        log.debug("Current URL after goto: %s", page.url)

        overall_timeout_s = 600.0 if login else 45.0
        deadline = time.monotonic() + overall_timeout_s

        def _job_page_ready_locator():
            # LinkedIn serves different job page shells depending on auth, AB tests, and headless.
            # Prefer a broad "page has job content" signal.
            return page.locator(
                ",".join(
                    [
                        # signed-in job view
                        "h1",
                        ".job-details-jobs-unified-top-card__job-title",
                        ".jobs-unified-top-card__job-title",
                        # newer/alternate job details shell (observed in headless)
                        "[componentkey^='JobDetails_']",
                    ]
                )
            ).first

        while True:
            if _is_login_or_checkpoint_url(page.url):
                log.info("LinkedIn redirected to %s", page.url)
                if not login:
                    context.close()
                    raise RuntimeError(
                        "Not logged in (LinkedIn redirected to a login/checkpoint page). "
                        "Re-run with `--login` to complete LinkedIn authentication."
                    )
                input(
                    "LinkedIn needs authentication (checkpoint/MFA). Complete it in the browser, "
                    "then press Enter here to continue..."
                )
                try:
                    page.goto(job_url, wait_until="domcontentloaded")
                except PlaywrightError as e:
                    if "interrupted" in str(e).lower() or "navigation" in str(e).lower():
                        log.info("Navigation was redirected (e.g. checkpoint); continuing. %s", e)
                    else:
                        raise
                log.debug("Current URL after post-login goto: %s", page.url)

            try:
                # Wait for job page shell to attach; "visible" is too strict across layouts.
                _job_page_ready_locator().wait_for(state="attached", timeout=5_000)
                break
            except PlaywrightTimeoutError:
                if time.monotonic() >= deadline:
                    try:
                        page.screenshot(
                            path=str(out_dir / "debug" / "single_job_load_failed.png"),
                            full_page=True,
                        )
                    except Exception:
                        pass
                    try:
                        (out_dir / "debug" / "single_job_load_failed.html").write_text(
                            page.content(), encoding="utf-8"
                        )
                    except Exception:
                        pass
                    url = page.url
                    context.close()
                    raise RuntimeError(
                        "Could not load job page before timeout. "
                        f"Current URL: {url!r}. LinkedIn DOM may have changed or you may still be on a checkpoint page."
                    )
                continue

        job_id = _parse_job_id_from_any(page.url) or _parse_job_id_from_any(job_url) or "unknown"

        already_applied, applied_text = _detect_already_applied(page=page, card=page.locator("body").first)

        # Scope top-card selectors to avoid matching global nav links.
        top = page.locator(
            ".job-details-jobs-unified-top-card__container, .jobs-unified-top-card, .jobs-details-top-card"
        ).first

        title = ""
        for sel in (
            ".job-details-jobs-unified-top-card__job-title",
            ".jobs-unified-top-card__job-title",
            "h1",
            "h2",
        ):
            try:
                el = top.locator(sel).first
                if el.count():
                    title = normalize_whitespace(el.inner_text(timeout=3_000))
                    if title:
                        break
            except Exception:
                continue
        if (not title) or title == "N/A":
            # Fallback: parse from <title> "Role | Company | LinkedIn" or "Company hiring Role in Location | LinkedIn"
            try:
                t = (page.title() or "").strip()
                if t and not _is_likely_list_page_title(t):
                    extracted = _extract_title_from_page_title(t)
                    if extracted:
                        title = extracted
            except Exception:
                pass
        if not title:
            title = "N/A"

        company = "N/A"
        company_url: str | None = None
        try:
            company_el = top.locator(
                ",".join(
                    [
                        "a.jobs-unified-top-card__company-name",
                        "a.jobs-unified-top-card__company-name-link",
                        "a[data-control-name='company_link']",
                        "span.jobs-unified-top-card__company-name",
                        "span.job-details-jobs-unified-top-card__company-name",
                        ".job-details-jobs-unified-top-card__company-name",
                        ".jobs-unified-top-card__company-name",
                        "a[href*='/company/']",
                        "[data-tracking-control-name='public_jobs_topcard-org-name']",
                        ".job-details-top-card__company-url",
                    ]
                )
            ).first
            if company_el.count():
                company = normalize_whitespace(company_el.inner_text(timeout=3_000))
                company_url = company_el.get_attribute("href")
        except Exception:
            pass
        if company == "N/A":
            # Fallback 1: text between title and location (consistent card format)
            try:
                header_text = page.locator("body").inner_text(timeout=2_000)
                header_text = (header_text or "")[:800]
                extracted = _extract_company_between_title_and_location(header_text, title)
                if extracted:
                    company = extracted
            except Exception:
                pass
        if company == "N/A":
            # Fallback 2: page title ("Role | Company | LinkedIn" or "Company hiring Role...")
            try:
                t = (page.title() or "").strip()
                extracted = _extract_company_from_title(t)
                if extracted:
                    company = extracted
            except Exception:
                pass
        if company == "N/A":
            # Fallback 3: URL slug (e.g. .../technical-support-engineer-at-remotehunter-4372308994)
            try:
                extracted = _extract_company_from_job_url(page.url or job_url)
                if extracted:
                    company = extracted
            except Exception:
                pass
        if company == "N/A":
            # Fallback 4: first 100 words of job canvas (top card + description)
            try:
                canvas_parts = []
                canvas_parts.append(top.inner_text(timeout=2_000))
                desc_loc = page.locator(
                    "div.jobs-description__content, div.jobs-description-content__text, div.jobs-box__html-content"
                ).first
                if desc_loc.count():
                    canvas_parts.append(desc_loc.inner_text(timeout=3_000))
                canvas_text = normalize_whitespace(" ".join(canvas_parts))
                blocklist = getattr(cfg.filters.company_blocklist, "exact", None) or []
                extracted = _extract_company_from_canvas_first_100_words(canvas_text, blocklist)
                if extracted:
                    company = extracted
            except Exception:
                pass
        if company == "N/A":
            # Fallback 5: parse description intro ("About Company –", "Company is hiring")
            try:
                canvas_parts_5 = []
                canvas_parts_5.append(top.inner_text(timeout=2_000))
                desc_loc_5 = page.locator(
                    "div.jobs-description__content, div.jobs-description-content__text, div.jobs-box__html-content"
                ).first
                if desc_loc_5.count():
                    canvas_parts_5.append(desc_loc_5.inner_text(timeout=3_000))
                canvas_text_5 = normalize_whitespace(" ".join(canvas_parts_5))
                extracted = _extract_company_from_description(canvas_text_5)
                if extracted:
                    company = extracted
            except Exception:
                pass
        if (company or "").strip().lower() == "linkedin" and not company_url:
            company = "N/A"

        company_logo_url: str | None = None
        try:
            company_logo_url = _extract_company_logo_url(top=top)
        except Exception:
            company_logo_url = None

        meta_text = ""
        try:
            meta_text = normalize_whitespace(
                top.locator(
                    ".jobs-unified-top-card__primary-description, .job-details-jobs-unified-top-card__primary-description"
                ).first.inner_text(timeout=3_000)
            )
        except Exception:
            meta_text = ""

        if not meta_text:
            try:
                bullets = top.locator(
                    ".jobs-unified-top-card__bullet, .job-details-jobs-unified-top-card__bullet"
                ).all_inner_texts()
                meta_text = normalize_whitespace(" ".join([normalize_whitespace(x) for x in (bullets or []) if x]))
            except Exception:
                meta_text = ""

        remote_status = _remote_status_from_meta(meta_text)

        bullets2: list[str] = []
        try:
            bullets2 = top.locator(
                ".jobs-unified-top-card__bullet, .job-details-jobs-unified-top-card__bullet"
            ).all_inner_texts()
        except Exception:
            bullets2 = []
        location_text = _pick_location_text(bullets=bullets2, meta_text=meta_text, company=company)

        date_posted, date_posted_text = _extract_date_posted(page=page)
        if not date_posted_text:
            # Fallback: search the whole page text for "X ago".
            try:
                body_text = normalize_whitespace(page.locator("body").inner_text(timeout=2_000))
                m = _POSTED_AGO_RE.search(body_text or "")
                if m:
                    date_posted_text = m.group(0)
                    date_posted = parse_linkedin_relative_time(date_posted_text)
            except Exception:
                pass

        description = ""
        try:
            desc_el = page.locator(
                "div.jobs-description__content, div.jobs-description-content__text, div.jobs-box__html-content"
            ).first
            if desc_el.count():
                description = desc_el.inner_text(timeout=10_000)
        except Exception:
            description = ""

        description = (description or "").strip()
        if not description:
            html = page.content()
            soup = BeautifulSoup(html, "html.parser")
            for bad in soup(["script", "style", "noscript"]):
                bad.decompose()
            description = soup.get_text(" ", strip=True)

        if not location_text and description:
            location_text = _extract_location_from_page_text(description)
        if not location_text:
            try:
                body_text = normalize_whitespace(page.locator("body").inner_text(timeout=2_000))
                location_text = _extract_location_from_page_text(body_text or "")
            except Exception:
                pass

        comp = _extract_compensation(page=page, description=description)

        canonical_url = f"https://www.linkedin.com/jobs/view/{job_id}" if job_id != "unknown" else job_url

        company_clean = (
            strip_location_from_company(company)
            if (company and str(company).strip() != "N/A")
            else (company or "N/A")
        )
        job = JobPost(
            job_id=str(job_id),
            job_url=canonical_url,
            title=title or "N/A",
            company=company_clean or "N/A",
            company_url=company_url,
            company_logo_url=company_logo_url,
            location_text=location_text,
            remote_status=remote_status,
            date_posted=date_posted,
            date_posted_text=date_posted_text,
            compensation=comp,
            description=description,
            already_applied=already_applied,
            applied_text=applied_text,
        )

        if seen_cache and job.job_id and job.job_id != "unknown":
            seen_cache.mark_seen(job_id=job.job_id, job_url=job.job_url, collection_name="single_job")
            seen_cache.mark_scanned(
                job_id=job.job_id,
                title=job.title or None,
                company=job.company or None,
                now=datetime.now(timezone.utc),
            )

        context.close()
        log.info("Single job scrape complete (job_id=%s)", job.job_id)
        return job

