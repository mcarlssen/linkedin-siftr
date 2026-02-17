from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from siftr.config import SeenCacheConfig

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    # ISO 8601 with timezone offset (UTC)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(s: str | None) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        # fromisoformat supports "+00:00" offsets
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except Exception:
        log.warning("Failed to read seen cache at %s; starting fresh.", path)
        return {}


def _save_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class SeenJobsCache:
    """
    Persistent cache keyed by LinkedIn job_id.

    The goal is to:
    - Avoid re-clicking / re-scraping the same cards every run
    - Record last-seen and last-scanned timestamps
    - Persist prefilter + AI outcomes for history/triage
    """

    def __init__(self, *, cfg: SeenCacheConfig, out_dir: str | Path):
        self.cfg = cfg
        out_dir = Path(out_dir)
        # Default behavior: keep cache inside run.out_dir.
        default_path = "out/cache/seen_jobs.json"
        if str(cfg.path).strip() == default_path:
            self.path = out_dir / "cache" / "seen_jobs.json"
        else:
            self.path = Path(cfg.path)
            # If configured path is relative, treat it as relative to CWD.
            # (Docs/config use relative paths by default.)
            if not self.path.is_absolute():
                self.path = Path(cfg.path)
        self._data: dict[str, Any] = _load_json(self.path) if cfg.enabled else {}
        self._dirty = False

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def get(self, job_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        v = self._data.get(job_id)
        return v if isinstance(v, dict) else None

    def get_last_scanned_at(self, job_id: str) -> datetime | None:
        rec = self.get(job_id) or {}
        return _parse_iso(rec.get("last_scanned_at_utc"))

    def should_skip_scan(self, job_id: str, *, now: datetime | None = None) -> bool:
        if not self.enabled:
            return False
        if int(self.cfg.skip_if_scanned_within_days) <= 0:
            return False
        rec = self.get(job_id) or {}
        last_scanned = _parse_iso(rec.get("last_scanned_at_utc"))
        if not last_scanned:
            return False
        now = now or _utcnow()
        age_days = (now - last_scanned).total_seconds() / 86400.0
        return age_days < float(self.cfg.skip_if_scanned_within_days)

    def mark_seen(
        self,
        *,
        job_id: str,
        job_url: str | None = None,
        collection_name: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if not self.enabled or not job_id:
            return
        now = now or _utcnow()
        rec = self.get(job_id) or {}
        if "job_id" not in rec:
            rec["job_id"] = job_id
        if job_url:
            u = str(job_url).strip()
            # Normalize relative LinkedIn URLs so downstream logs are clickable.
            if u.startswith("/"):
                u = "https://www.linkedin.com" + u
            elif u.startswith("jobs/"):
                u = "https://www.linkedin.com/" + u
            rec["job_url"] = u
        if not rec.get("first_seen_at_utc"):
            rec["first_seen_at_utc"] = _iso(now)
        rec["last_seen_at_utc"] = _iso(now)

        if collection_name:
            cols = rec.get("collections")
            if not isinstance(cols, dict):
                cols = {}
            c = cols.get(collection_name)
            if not isinstance(c, dict):
                c = {}
            if not c.get("first_seen_at_utc"):
                c["first_seen_at_utc"] = _iso(now)
            c["last_seen_at_utc"] = _iso(now)
            cols[collection_name] = c
            rec["collections"] = cols

        self._data[job_id] = rec
        self._dirty = True

    def mark_scanned(
        self,
        *,
        job_id: str,
        title: str | None = None,
        company: str | None = None,
        now: datetime | None = None,
    ) -> None:
        if not self.enabled or not job_id:
            return
        now = now or _utcnow()
        rec = self.get(job_id) or {"job_id": job_id}
        rec["last_scanned_at_utc"] = _iso(now)
        if title:
            rec["title"] = title
        if company:
            rec["company"] = company
        self._data[job_id] = rec
        self._dirty = True

    def update_prefilter(
        self,
        *,
        job_id: str,
        run_at_utc: datetime,
        passed: bool,
        reasons: list[str] | None,
    ) -> None:
        if not self.enabled or not job_id:
            return
        rec = self.get(job_id) or {"job_id": job_id}
        rec["prefilter"] = {
            "last_run_at_utc": _iso(run_at_utc),
            "passed": bool(passed),
            "reasons": list(reasons or []),
        }
        self._data[job_id] = rec
        self._dirty = True

    def update_ai(
        self,
        *,
        job_id: str,
        run_at_utc: datetime,
        model: str | None,
        output: dict[str, Any] | None,
        cache_hit: bool,
        input_hash: str | None = None,
    ) -> None:
        if not self.enabled or not job_id:
            return
        out = output or {}
        # Match the same "prefer parsed" logic used elsewhere:
        parsed = out.get("parsed") if isinstance(out.get("parsed"), dict) else None
        src = parsed or out
        kill_criteria: list[str] | None = None
        verdict: str | None = None
        try:
            v = src.get("verdict")
            verdict = str(v).strip().upper() if isinstance(v, str) else None
        except Exception:
            verdict = None
        try:
            kc = src.get("kill_criteria")
            if isinstance(kc, list):
                kill_criteria = [str(x).strip() for x in kc if str(x).strip()]
        except Exception:
            kill_criteria = None

        rec = self.get(job_id) or {"job_id": job_id}
        rec["ai"] = {
            "last_run_at_utc": _iso(run_at_utc),
            "model": str(model) if model else None,
            "verdict": verdict,
            "kill_criteria": kill_criteria,
            "input_hash": str(input_hash) if input_hash else None,
            "_cache_hit": bool(cache_hit),
        }
        self._data[job_id] = rec
        self._dirty = True

    def save(self) -> None:
        if not self.enabled:
            return
        if not self._dirty:
            return
        _save_json_atomic(self.path, self._data)
        self._dirty = False
