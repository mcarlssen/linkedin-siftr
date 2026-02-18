from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from siftr.export_common import flatten_job
from siftr.models import JobPost
from siftr.util import ensure_dir


def export_jobs_xlsx(
    *,
    xlsx_path: str | Path,
    all_jobs: list[JobPost],
    passed_jobs: list[JobPost],
    run_meta: dict[str, Any],
) -> None:
    xlsx_path = Path(xlsx_path)
    ensure_dir(xlsx_path.parent)

    df_all = pd.DataFrame([flatten_job(j) for j in all_jobs])
    df_passed = pd.DataFrame([flatten_job(j) for j in passed_jobs])
    df_meta = pd.DataFrame([run_meta])

    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        df_meta.to_excel(writer, sheet_name="run_summary", index=False)
        df_all.to_excel(writer, sheet_name="all_jobs", index=False)
        df_passed.to_excel(writer, sheet_name="passed_ai", index=False)

