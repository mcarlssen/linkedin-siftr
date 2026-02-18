from __future__ import annotations

from dataclasses import asdict
from typing import Any

from siftr.models import JobPost


def flatten_job(job: JobPost) -> dict[str, Any]:
    c = asdict(job.compensation)
    return {
        "job_id": job.job_id,
        "job_url": job.job_url,
        "title": job.title,
        "company": job.company,
        "company_url": job.company_url,
        "company_logo_url": getattr(job, "company_logo_url", None),
        "location_text": job.location_text,
        "remote_status": job.remote_status,
        "date_posted": job.date_posted.isoformat() if job.date_posted else None,
        "date_posted_text": job.date_posted_text,
        "comp_interval": c.get("interval"),
        "comp_min": c.get("min_amount"),
        "comp_max": c.get("max_amount"),
        "comp_currency": c.get("currency"),
        "comp_min_annual_usd": c.get("min_annual_usd"),
        "comp_max_annual_usd": c.get("max_annual_usd"),
        "comp_min_hourly_usd": c.get("min_hourly_usd"),
        "comp_max_hourly_usd": c.get("max_hourly_usd"),
        "already_applied": job.already_applied,
        "applied_text": job.applied_text,
        "prefilter_pass": job.prefilter_pass,
        "prefilter_reasons": ";".join(job.prefilter_reasons or []),
        "ai_verdict": job.ai_verdict,
        "ai_kill_criteria": job.ai_kill_criteria,
        "ai_summary": job.ai_summary,
        "ai_output": job.ai_output,
        "description": job.description,
    }
