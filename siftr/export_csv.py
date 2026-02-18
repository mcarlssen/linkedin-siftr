from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from siftr.export_common import flatten_job
from siftr.models import JobPost
from siftr.util import ensure_dir


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
    df_all = pd.DataFrame([flatten_job(j) for j in all_jobs])
    passed_rows = [flatten_job(j) for j in passed_jobs]
    # The "passed_ai" export is meant for quick triage; omit large text blobs.
    for r in passed_rows:
        r.pop("description", None)
    df_passed = pd.DataFrame(passed_rows)

    df_meta.to_csv(p_meta, index=False, encoding="utf-8")
    df_all.to_csv(p_all, index=False, encoding="utf-8")
    df_passed.to_csv(p_passed, index=False, encoding="utf-8")

    return [p_meta, p_all, p_passed]

