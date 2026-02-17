from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal


RemoteStatus = Literal["remote", "hybrid", "onsite", "unknown"]
PayInterval = Literal["yearly", "hourly", "unknown"]


@dataclass
class Compensation:
    interval: PayInterval = "unknown"
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None

    # normalized convenience fields (computed later)
    min_annual_usd: float | None = None
    max_annual_usd: float | None = None
    min_hourly_usd: float | None = None
    max_hourly_usd: float | None = None


@dataclass
class JobPost:
    job_id: str
    job_url: str
    title: str
    company: str
    company_url: str | None
    company_logo_url: str | None
    location_text: str | None
    remote_status: RemoteStatus
    date_posted: datetime | None
    compensation: Compensation
    description: str

    # scraped flags
    date_posted_text: str | None = None
    already_applied: bool | None = None
    applied_text: str | None = None

    # pipeline outputs
    prefilter_pass: bool | None = None
    prefilter_reasons: list[str] | None = None
    ai_verdict: str | None = None
    ai_kill_criteria: str | None = None
    ai_summary: str | None = None
    ai_output: str | None = None

