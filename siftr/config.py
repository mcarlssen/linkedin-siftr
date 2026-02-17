from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


@dataclass(frozen=True)
class RateLimitConfig:
    min_delay_seconds: float = 1.0
    max_delay_seconds: float = 3.0


@dataclass(frozen=True)
class BrowserConfig:
    user_data_dir: str = "out/profiles/chromium"
    headless: bool = True
    slow_mo_ms: int = 75


@dataclass(frozen=True)
class ScrapeConfig:
    browser: BrowserConfig
    rate_limit: RateLimitConfig
    max_jobs: int = 200
    seen_cache: "SeenCacheConfig" = field(default_factory=lambda: SeenCacheConfig())


@dataclass(frozen=True)
class SeenCacheConfig:
    enabled: bool = True
    path: str = "out/cache/seen_jobs.json"
    skip_if_scanned_within_days: int = 7


@dataclass(frozen=True)
class CompConfig:
    allow_missing: bool = True
    min_annual_usd: int = 75_000
    min_hourly_usd: int = 38


RuleAction = Literal["skip"]


@dataclass(frozen=True)
class RuleCondition:
    field: str
    op: str
    value: Any | None = None


@dataclass(frozen=True)
class SkipRule:
    id: str
    when: list[RuleCondition]
    action: RuleAction
    reason: str


@dataclass(frozen=True)
class CompanyBlocklist:
    exact: list[str]
    regex: list[str]


@dataclass(frozen=True)
class FiltersConfig:
    remote_only: bool
    usa_only: bool
    compensation: CompConfig
    company_blocklist: CompanyBlocklist
    skip_rules: list[SkipRule]
    skip_already_applied: bool = True
    max_age_days: int | None = None
    allow_missing_date_posted: bool = True


@dataclass(frozen=True)
class AIConfig:
    provider: Literal["anthropic"]
    model: str
    max_tokens: int
    temperature: float
    resume_path: str


@dataclass(frozen=True)
class ExportConfig:
    xlsx_path: str | None = None
    csv_path: str | None = None
    datestamp: bool = False
    datestamp_format: str = "%Y%m%d-%H%M%S"


@dataclass(frozen=True)
class RunConfig:
    out_dir: str
    top_applicant_url: str | None = None
    recommended_url: str | None = None
    # Optional flexible set of named collection URLs to scrape.
    # Example:
    #   collections:
    #     top_applicant: "https://www.linkedin.com/jobs/collections/top-applicant/"
    #     recommended: "https://www.linkedin.com/jobs/collections/recommended/"
    collections: dict[str, str] | None = None
    max_ai_evals_per_run: int = 30


@dataclass(frozen=True)
class NotificationsConfig:
    """
    Notification config (initially email via Resend).

    Note: API keys are best supplied via environment variables in production.
    We still support config-driven keys to keep it flexible for future productization.
    """

    enabled: bool = False
    provider: Literal["resend"] = "resend"

    # Resend
    resend_api_key: str | None = None
    from_email: str | None = None
    to_emails: list[str] = field(default_factory=list)

    # Subject supports {new_ai_count} and common run_meta keys.
    subject: str = "LinkedIn job eval - {new_ai_count} new AI result(s)"


@dataclass(frozen=True)
class AppConfig:
    run: RunConfig
    scrape: ScrapeConfig
    filters: FiltersConfig
    ai: AIConfig
    export: ExportConfig
    notifications: NotificationsConfig


def _require(d: dict[str, Any], key: str) -> Any:
    if key not in d:
        raise ValueError(f"Missing required config key: {key}")
    return d[key]


def _resolve_path(value: str, config_root: Path) -> str:
    """Resolve relative paths against config file directory so scheduled tasks use the same paths."""
    if not value or Path(value).is_absolute():
        return value
    return str((config_root / value).resolve())


def load_config(path: str | Path) -> AppConfig:
    path = Path(path)
    config_root = path.resolve().parent
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a mapping/dict.")

    run_raw = _require(raw, "run")
    scrape_raw = _require(raw, "scrape")
    filters_raw = _require(raw, "filters")
    ai_raw = _require(raw, "ai")
    export_raw = _require(raw, "export")
    notifications_raw = raw.get("notifications") or {}
    if not isinstance(export_raw, dict):
        raise ValueError("export must be a mapping/dict.")
    if not isinstance(run_raw, dict):
        raise ValueError("run must be a mapping/dict.")
    if notifications_raw and not isinstance(notifications_raw, dict):
        raise ValueError("notifications must be a mapping/dict.")

    browser_raw = _require(scrape_raw, "browser")
    rate_raw = _require(scrape_raw, "rate_limit")
    max_jobs = int(scrape_raw.get("max_jobs", 200))
    seen_cache_raw = scrape_raw.get("seen_cache") or {}
    if seen_cache_raw and not isinstance(seen_cache_raw, dict):
        raise ValueError("scrape.seen_cache must be a mapping/dict.")
    if isinstance(seen_cache_raw, dict) and seen_cache_raw.get("path"):
        seen_cache_raw = {**seen_cache_raw, "path": _resolve_path(str(seen_cache_raw["path"]), config_root)}

    comp_raw = _require(filters_raw, "compensation")
    block_raw = _require(filters_raw, "company_blocklist")
    skip_rules_raw = filters_raw.get("skip_rules", []) or []

    skip_rules: list[SkipRule] = []
    for r in skip_rules_raw:
        when_raw = r.get("when", []) or []
        when = [RuleCondition(**c) for c in when_raw]
        skip_rules.append(
            SkipRule(
                id=_require(r, "id"),
                when=when,
                action=_require(r, "action"),
                reason=_require(r, "reason"),
            )
        )

    # Build a flexible collections map from:
    # - run.collections (preferred)
    # - any keys ending in "_url" (back-compat and easy extension)
    collections: dict[str, str] = {}
    raw_collections = run_raw.get("collections") or {}
    if raw_collections:
        if not isinstance(raw_collections, dict):
            raise ValueError("run.collections must be a mapping/dict.")
        for k, v in raw_collections.items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                collections[k.strip()] = v.strip()

    for k, v in run_raw.items():
        if isinstance(k, str) and k.endswith("_url") and isinstance(v, str) and v.strip():
            name = k[: -len("_url")].strip()
            if name:
                collections.setdefault(name, v.strip())

    run_cfg = RunConfig(
        out_dir=_resolve_path(str(_require(run_raw, "out_dir")), config_root),
        top_applicant_url=(str(run_raw.get("top_applicant_url")).strip() if run_raw.get("top_applicant_url") else None),
        recommended_url=(str(run_raw.get("recommended_url")).strip() if run_raw.get("recommended_url") else None),
        collections=collections or None,
        max_ai_evals_per_run=int(run_raw.get("max_ai_evals_per_run", 30)),
    )
    if not (run_cfg.collections or run_cfg.top_applicant_url or run_cfg.recommended_url):
        raise ValueError("run must provide at least one collection URL (e.g. run.top_applicant_url or run.collections).")

    # notifications: normalize to_emails into a list[str]
    to_val = (
        notifications_raw.get("to_emails")
        if isinstance(notifications_raw, dict)
        else None
    )
    if to_val is None and isinstance(notifications_raw, dict):
        to_val = notifications_raw.get("to")
    if to_val is None and isinstance(notifications_raw, dict):
        to_val = notifications_raw.get("to_email")

    to_emails: list[str] = []
    if isinstance(to_val, str) and to_val.strip():
        to_emails = [to_val.strip()]
    elif isinstance(to_val, list):
        to_emails = [str(x).strip() for x in to_val if str(x).strip()]

    notifications_cfg = NotificationsConfig(
        enabled=bool(notifications_raw.get("enabled", False)) if isinstance(notifications_raw, dict) else False,
        provider=str(notifications_raw.get("provider", "resend")).strip() if isinstance(notifications_raw, dict) else "resend",
        resend_api_key=(str(notifications_raw.get("resend_api_key")).strip() if notifications_raw.get("resend_api_key") else None)
        if isinstance(notifications_raw, dict)
        else None,
        from_email=(str(notifications_raw.get("from_email")).strip() if notifications_raw.get("from_email") else None)
        if isinstance(notifications_raw, dict)
        else None,
        to_emails=to_emails,
        subject=str(notifications_raw.get("subject", NotificationsConfig.subject)).strip()
        if isinstance(notifications_raw, dict)
        else NotificationsConfig.subject,
    )

    browser_user_data_dir = _resolve_path(
        str(browser_raw.get("user_data_dir", "out/profiles/chromium")), config_root
    )
    cfg = AppConfig(
        run=run_cfg,
        scrape=ScrapeConfig(
            browser=BrowserConfig(**{**browser_raw, "user_data_dir": browser_user_data_dir}),
            rate_limit=RateLimitConfig(**rate_raw),
            max_jobs=max_jobs,
            seen_cache=SeenCacheConfig(**seen_cache_raw) if isinstance(seen_cache_raw, dict) else SeenCacheConfig(),
        ),
        filters=FiltersConfig(
            remote_only=bool(_require(filters_raw, "remote_only")),
            usa_only=bool(_require(filters_raw, "usa_only")),
            skip_already_applied=bool(filters_raw.get("skip_already_applied", True)),
            max_age_days=(int(filters_raw["max_age_days"]) if "max_age_days" in filters_raw and filters_raw["max_age_days"] is not None else None),
            allow_missing_date_posted=bool(filters_raw.get("allow_missing_date_posted", True)),
            compensation=CompConfig(**comp_raw),
            company_blocklist=CompanyBlocklist(
                exact=list(block_raw.get("exact", []) or []),
                regex=list(block_raw.get("regex", []) or []),
            ),
            skip_rules=skip_rules,
        ),
        ai=AIConfig(**ai_raw),
        export=ExportConfig(**export_raw),
        notifications=notifications_cfg,
    )

    if not (cfg.export.xlsx_path or cfg.export.csv_path):
        raise ValueError("export must specify at least one of: xlsx_path, csv_path")

    return cfg

