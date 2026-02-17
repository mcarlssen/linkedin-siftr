from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def jitter_sleep(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def dump_json(path: str | Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def configure_logging(*, verbose: bool = False) -> None:
    """
    Configure console logging for the CLI.

    - Default: INFO-level progress.
    - Verbose: still INFO-level (slightly more output comes from explicit INFO logs).
    """
    # Keep console output readable: INFO by default and in --verbose.
    # If you want full debug output, add a dedicated CLI flag and wire it here.
    level = logging.INFO

    # Avoid duplicate handlers if configure_logging is called twice.
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level)
        return

    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Reduce noise from dependencies at INFO/DEBUG.
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


def parse_linkedin_relative_time(text: str) -> datetime | None:
    """
    Parses strings like:
    - "1 day ago"
    - "Reposted 3 days ago"
    - "2 weeks ago"
    Returns an approximate UTC datetime.
    """
    if not text:
        return None
    s = text.strip().lower()
    s = s.replace("reposted", "").strip()
    m = re.search(r"(\d+)\s+(minute|minutes|hour|hours|day|days|week|weeks|month|months)\s+ago", s)
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2)
    delta: timedelta
    if unit.startswith("minute"):
        delta = timedelta(minutes=n)
    elif unit.startswith("hour"):
        delta = timedelta(hours=n)
    elif unit.startswith("day"):
        delta = timedelta(days=n)
    elif unit.startswith("week"):
        delta = timedelta(weeks=n)
    elif unit.startswith("month"):
        delta = timedelta(days=30 * n)
    else:
        return None
    return datetime.now(timezone.utc) - delta


def normalize_whitespace(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


# Strip trailing location from company (e.g. "Jobgether United States (" -> "Jobgether").
_COMPANY_TRAILING_LOCATION_RE = re.compile(
    r"\s+"
    r"(?:"
    r"United\s+States|Canada|UK|United\s+Kingdom|"
    r"Remote|Hybrid|On[-\s]?site|Onsite|"
    r"(?![A-Za-z\s]*USA\s)[A-Z][a-zA-Z\s\-]+,\s*[A-Z]{2}"  # City, ST (not "USA City")
    r")"
    r"(?:\s*\([^)]*)?$",
    re.IGNORECASE,
)


def strip_location_from_company(company: str | None) -> str:
    """
    Remove trailing location from company string. LinkedIn sometimes concatenates
    "Company Location" or "Company Location (3 days ago" in a single element.
    E.g. "Jobgether United States (" -> "Jobgether", "Canopy USA Cleveland, OH (" -> "Canopy USA".
    """
    if not company or not (company := company.strip()):
        return company or ""
    prev = None
    while prev != company:
        prev = company
        company = _COMPANY_TRAILING_LOCATION_RE.sub("", company).strip().rstrip("·•|(-")
    return company.strip() if company else prev or ""

