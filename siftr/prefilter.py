from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from siftr.config import FiltersConfig, SkipRule
from siftr.models import JobPost
from siftr.util import parse_linkedin_relative_time


US_MARKERS = [
    "united states",
    "u.s.",
    "usa",
]

US_STATE_ABBRS = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA","KS","KY","LA","ME","MD","MA","MI","MN",
    "MS","MO","MT","NE","NV","NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA",
    "WA","WV","WI","WY","DC",
}


def _text_has_usa(s: str) -> bool:
    t = (s or "").lower()
    if any(m in t for m in US_MARKERS):
        return True
    # State abbreviation heuristic.
    #
    # IMPORTANT: don't match bare tokens like "in" / "or" in normal prose.
    # Only treat it as a US signal when it appears in common location formats:
    #   - "Chicago, IL"
    #   - "(IL)"
    state_pat = r"(" + "|".join(sorted(US_STATE_ABBRS)) + r")"
    if re.search(r"(?:,\s*|\(\s*)" + state_pat + r"\b", s or "", flags=re.IGNORECASE):
        return True
    return False


def _is_remote_only(job: JobPost) -> bool:
    if job.remote_status == "remote":
        return True
    if job.remote_status in ("hybrid", "onsite"):
        return False
    # unknown: inspect location + description (avoid false positives like "remote support")
    loc = (job.location_text or "").lower()
    d = (job.description or "").lower()

    negative_markers = [
        "on-site",
        "onsite",
        "on site",
        "in-office",
        "in office",
        "100% in office",
        "this role is 100% in office",
        "must be in office",
        "hybrid",
    ]
    if any(m in loc for m in negative_markers) or any(m in d for m in negative_markers):
        return False

    # Prefer false-positives over false-negatives:
    # If LinkedIn doesn't label the role explicitly as Remote/Hybrid/On-site, but the location is a
    # broad US-wide label (e.g. "United States") with no city/state granularity, treat it as remote.
    #
    # This is intentionally permissive: it reduces missed remote roles at the cost of letting some
    # ambiguous "US-based" postings through.
    if _text_has_usa(job.location_text or ""):
        # If it looks like a specific locale ("City, ST" or "(ST)"), don't auto-classify as remote.
        state_pat = r"(" + "|".join(sorted(US_STATE_ABBRS)) + r")"
        if not ("," in (job.location_text or "")) and not re.search(
            r"(?:,\s*|\(\s*)" + state_pat + r"\b",
            job.location_text or "",
            flags=re.IGNORECASE,
        ):
            return True

    # Positive remote signals (keep these relatively strict).
    positive_phrases = [
        "fully remote",
        "100% remote",
        "remote-first",
        "remote first",
        "work from home",
        "wfh",
    ]
    if any(p in loc for p in positive_phrases) or any(p in d for p in positive_phrases):
        return True

    # Generic "remote" token: treat as remote only when it looks like a location label,
    # not when it describes support modality ("support remotely", "remote support").
    if re.search(r"\bremote\b", loc):
        return True
    if re.search(r"\bremote\b", d):
        if re.search(r"\bremote\s+support\b", d) or re.search(r"\bsupport\s+remotely\b", d):
            return False
        return True

    return False


def _normalize_comp(job: JobPost) -> None:
    """
    Populate normalized USD convenience fields on job.compensation.
    We only support USD heuristics for now since your criteria are US-only.
    """
    c = job.compensation
    if not c or c.min_amount is None:
        return
    if c.interval == "yearly":
        c.min_annual_usd = float(c.min_amount)
        c.max_annual_usd = float(c.max_amount) if c.max_amount is not None else None
    elif c.interval == "hourly":
        c.min_hourly_usd = float(c.min_amount)
        c.max_hourly_usd = float(c.max_amount) if c.max_amount is not None else None


def _passes_comp(job: JobPost, *, min_annual: int, min_hourly: int, allow_missing: bool) -> bool:
    c = job.compensation
    if not c or c.min_amount is None or c.interval == "unknown":
        return bool(allow_missing)

    _normalize_comp(job)

    if c.interval == "yearly":
        # Accept ranges that *reach* the threshold, not only those whose floor exceeds it.
        v = c.max_annual_usd if c.max_annual_usd is not None else c.min_annual_usd
        return float(v or 0) >= float(min_annual)
    if c.interval == "hourly":
        v = c.max_hourly_usd if c.max_hourly_usd is not None else c.min_hourly_usd
        return float(v or 0) >= float(min_hourly)
    return bool(allow_missing)


def _field_value(job: JobPost, field: str) -> Any:
    if field == "title":
        return job.title
    if field == "company":
        return job.company
    if field == "location_text":
        return job.location_text
    if field == "description":
        return job.description
    if field == "is_remote":
        # map to tri-state string
        if job.remote_status == "remote":
            return True
        if job.remote_status in ("hybrid", "onsite"):
            return False
        return None
    if field == "comp_min_annual_usd":
        _normalize_comp(job)
        return job.compensation.min_annual_usd
    if field == "comp_max_annual_usd":
        _normalize_comp(job)
        return job.compensation.max_annual_usd
    if field == "comp_min_hourly_usd":
        _normalize_comp(job)
        return job.compensation.min_hourly_usd
    if field == "comp_max_hourly_usd":
        _normalize_comp(job)
        return job.compensation.max_hourly_usd
    return None


def _cond_match(value: Any, op: str, expected: Any) -> bool:
    if op == "contains":
        return isinstance(value, str) and str(expected).lower() in value.lower()
    if op == "regex":
        return isinstance(value, str) and re.search(str(expected), value) is not None
    if op == "equals":
        return value == expected
    if op == "in":
        return value in (expected or [])
    if op in ("gte", "gt", "lte", "lt"):
        if value is None:
            return False
        try:
            v = float(value)
            e = float(expected)
        except Exception:
            return False
        if op == "gte":
            return v >= e
        if op == "gt":
            return v > e
        if op == "lte":
            return v <= e
        if op == "lt":
            return v < e
    if op == "is_true":
        return value is True
    if op == "is_false":
        return value is False
    if op == "is_unknown":
        return value is None
    return False


def _rule_matches(job: JobPost, rule: SkipRule) -> bool:
    for cond in rule.when:
        value = _field_value(job, cond.field)
        if not _cond_match(value, cond.op, cond.value):
            return False
    return True


def apply_prefilters(job: JobPost, cfg: FiltersConfig) -> tuple[bool, list[str]]:
    """
    Returns (pass, reasons). Reasons are *reject* reasons when pass=False,
    or informational reasons when pass=True.
    """
    reasons: list[str] = []

    # skip already-applied jobs (saves AI tokens)
    if getattr(cfg, "skip_already_applied", True):
        if job.already_applied is True:
            return False, ["reject.already_applied"]

    # max age filter (skip older postings)
    max_age = getattr(cfg, "max_age_days", None)
    if max_age is not None:
        # Robustness: sometimes we scrape `date_posted_text` but fail to parse `date_posted`.
        # Try parsing from the text before treating it as missing.
        if job.date_posted is None and getattr(job, "date_posted_text", None):
            try:
                dt = parse_linkedin_relative_time(str(getattr(job, "date_posted_text") or ""))
                if dt is not None:
                    job.date_posted = dt
            except Exception:
                pass

        if job.date_posted is None:
            if not getattr(cfg, "allow_missing_date_posted", True):
                return False, ["reject.posted_date_missing"]
        else:
            age_days = (datetime.now(timezone.utc) - job.date_posted).total_seconds() / 86400.0
            if age_days > float(max_age):
                return False, [f"reject.posted_too_old.{int(max_age)}d"]

    # company blocklist (substring match for exact, regex for patterns)
    company_raw = (job.company or "").strip()
    for bl in (cfg.company_blocklist.exact or []):
        s = str(bl).strip()
        if s and s.lower() in (company_raw or "").lower():
            return False, ["reject.company_blocked.exact"]
    for pat in cfg.company_blocklist.regex:
        try:
            if re.search(pat, company_raw or ""):
                return False, ["reject.company_blocked.regex"]
        except re.error:
            # ignore malformed regex patterns
            continue

    # USA-only
    if cfg.usa_only:
        loc_blob = f"{job.location_text or ''} {job.description or ''}"
        if not _text_has_usa(loc_blob):
            return False, ["reject.not_usa"]

    # remote-only
    if cfg.remote_only:
        if not _is_remote_only(job):
            return False, ["reject.not_remote"]

    # compensation
    if not _passes_comp(
        job,
        min_annual=cfg.compensation.min_annual_usd,
        min_hourly=cfg.compensation.min_hourly_usd,
        allow_missing=cfg.compensation.allow_missing,
    ):
        return False, ["reject.comp_too_low"]

    # skip rules
    for rule in cfg.skip_rules:
        if rule.action == "skip" and _rule_matches(job, rule):
            return False, [f"reject.skip_rule.{rule.id}"]

    return True, reasons

