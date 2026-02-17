from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

import pandas as pd

from siftr.models import JobPost
from siftr.util import ensure_dir


def _flatten_job(job: JobPost) -> dict[str, Any]:
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


def export_jobs_csv(
    *,
    csv_path: str | Path,
    all_jobs: list[JobPost],
    passed_jobs: list[JobPost],
    run_meta: dict[str, Any],
) -> list[Path]:
    """
    CSV equivalent of the XLSX export.

    Since CSV has no sheets, we write three CSVs derived from the provided path:
    - <stem>.run_summary.csv
    - <stem>.all_jobs.csv
    - <stem>.passed_ai.csv
    """
    csv_path = Path(csv_path)
    ensure_dir(csv_path.parent)

    stem = csv_path.with_suffix("")  # drop .csv if present; no-op otherwise
    p_meta = stem.with_suffix(".run_summary.csv")
    p_all = stem.with_suffix(".all_jobs.csv")
    p_passed = stem.with_suffix(".passed_ai.csv")

    df_meta = pd.DataFrame([run_meta])
    df_all = pd.DataFrame([_flatten_job(j) for j in all_jobs])
    passed_rows = [_flatten_job(j) for j in passed_jobs]
    # The "passed_ai" export is meant for quick triage; omit large text blobs.
    for r in passed_rows:
        r.pop("description", None)
    df_passed = pd.DataFrame(passed_rows)

    df_meta.to_csv(p_meta, index=False, encoding="utf-8")
    df_all.to_csv(p_all, index=False, encoding="utf-8")
    df_passed.to_csv(p_passed, index=False, encoding="utf-8")

    return [p_meta, p_all, p_passed]

