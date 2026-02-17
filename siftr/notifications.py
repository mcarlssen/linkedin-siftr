from __future__ import annotations

import html
import logging
import os
import re
from dataclasses import asdict
from datetime import datetime
from typing import Any

from siftr.config import NotificationsConfig
from siftr.models import JobPost

log = logging.getLogger("siftr.notifications")

# Extract clean location from AI output that may include page header/nav junk.
_LOCATION_REMOTE_RE = re.compile(
    r"\b(Remote|Hybrid|On-site|On site|Onsite)(?:\s*\([^)]+\))?",
    re.IGNORECASE,
)
_LOCATION_CITY_ST_RE = re.compile(
    r"\b([A-Z][a-zA-Z\s\-]+),\s*([A-Z]{2})(?:\s+\d{5})?\b",
)


def _extract_clean_location(text: str) -> str | None:
    """
    Extract a clean location string from text that may contain page header/nav junk
    (e.g. "Skip to main content Home My Network Jobs ... New York, NY").
    Returns only the location part (e.g. "New York, NY" or "Hybrid (Mon-Thu on-site)").
    """
    if not text or not text.strip():
        return None
    s = " ".join(text.split())
    m = _LOCATION_REMOTE_RE.search(s)
    if m:
        return m.group(0).strip()
    m = _LOCATION_CITY_ST_RE.search(s)
    if m:
        return f"{m.group(1).strip()}, {m.group(2)}"
    return None


def _fmt_dt(v: Any) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    if v is None:
        return ""
    return str(v)


def _fmt_bool(v: Any) -> str:
    if v is True:
        return "true"
    if v is False:
        return "false"
    return ""


def _sanitize_ai_job_facts(v: Any) -> dict[str, Any] | None:
    """
    Extract and sanitize the AI's `job_facts_extracted` for inclusion in email.
    We explicitly avoid including any `raw_text` blobs.
    """
    if not isinstance(v, dict):
        return None
    src = v.get("parsed") if isinstance(v.get("parsed"), dict) else v
    jfe = src.get("job_facts_extracted")
    if not isinstance(jfe, dict):
        return None

    out: dict[str, Any] = {}
    for k, val in jfe.items():
        ks = str(k)
        if "raw" in ks.lower() or "description" in ks.lower():
            continue
        if val is None or isinstance(val, (bool, int, float)):
            out[ks] = val
        elif isinstance(val, str):
            s = " ".join(val.split())
            if len(s) > 300:
                s = s[:300].rstrip() + "…"
            out[ks] = s
        # skip nested objects/arrays by default (keeps the email small)
    return out or None


def _extract_ai_core_mission(v: Any) -> str | None:
    """
    Pull a concise "core mission" / Role Summary from the AI output if present.
    Tries real_job_decoded first, then job_facts_extracted so more jobs show a Role Summary.
    """
    if not isinstance(v, dict):
        return None
    src = v.get("parsed") if isinstance(v.get("parsed"), dict) else v
    rjd = src.get("real_job_decoded") if isinstance(src.get("real_job_decoded"), dict) else None
    if rjd:
        for k in ("core_mission", "core_mandate", "actual_role", "core_mandate_summary", "core_mandate_decoded"):
            val = rjd.get(k)
            if isinstance(val, str) and val.strip():
                return " ".join(val.split())
    jfe = src.get("job_facts_extracted") if isinstance(src.get("job_facts_extracted"), dict) else None
    if jfe:
        for k in ("actual_role", "role_summary", "core_mission", "title", "critical_gap"):
            val = jfe.get(k)
            if isinstance(val, str) and val.strip() and len(val.strip()) > 10:
                return " ".join(val.split())
    return None


def _fmt_compensation_short(comp: dict[str, Any] | None) -> str:
    """
    Compact, human-readable compensation for email, e.g.:
    - "$110–135k"
    - "$55–70/hr"
    """
    if not isinstance(comp, dict):
        return ""

    interval = str(comp.get("interval") or "unknown").strip().lower()
    currency = str(comp.get("currency") or "").strip().upper()
    min_amount = comp.get("min_amount")
    max_amount = comp.get("max_amount")

    try:
        mn = float(min_amount) if min_amount is not None and str(min_amount).strip() != "" else None
        mx = float(max_amount) if max_amount is not None and str(max_amount).strip() != "" else None
    except Exception:
        mn, mx = None, None

    if mn is None and mx is None:
        return ""

    # Currency symbol (email is US-focused for now).
    sym = "$" if (not currency or currency in ("USD", "$", "US$")) else f"{currency} "

    # Hourly vs yearly: use explicit interval, else heuristic.
    is_hourly = interval == "hourly"
    if interval == "unknown":
        # heuristic: typical hourly is < $500
        is_hourly = (mn is not None and mn < 500) and (mx is None or mx < 500)

    if is_hourly:
        a = int(round(mn)) if mn is not None else None
        b = int(round(mx)) if mx is not None else None
        if a is not None and b is not None and b != a:
            return f"{sym}{a}-{b}/hr"
        if a is not None:
            return f"{sym}{a}/hr"
        return f"{sym}{b}/hr" if b is not None else ""

    # Default: treat as annual-ish and compact to k when large.
    def _to_k(v: float) -> str:
        k = v / 1000.0
        # Prefer integer k when plausible
        kk = int(round(k))
        if abs(k - kk) < 0.05:
            return str(kk)
        s = f"{k:.1f}".rstrip("0").rstrip(".")
        return s

    if mn is not None and mx is not None and mx != mn and mn >= 1000 and mx >= 1000:
        return f"{sym}{_to_k(mn)}-{_to_k(mx)}k"
    if mn is not None and mn >= 1000:
        return f"{sym}{_to_k(mn)}k"
    if mx is not None and mx >= 1000:
        return f"{sym}{_to_k(mx)}k"

    # Fallback small numbers (rare for salary; could be unknown interval)
    a = int(round(mn)) if mn is not None else None
    b = int(round(mx)) if mx is not None else None
    if a is not None and b is not None and b != a:
        return f"{sym}{a}–{b}"
    if a is not None:
        return f"{sym}{a}"
    return f"{sym}{b}" if b is not None else ""


def _job_metadata_dict(job: JobPost, *, ai_output: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Return "all metadata" for a job EXCEPT:
    - description
    - ai_output (can contain huge raw_text / parsed blobs)
    """
    c = asdict(job.compensation)
    base: dict[str, Any] = {
        "job_id": job.job_id,
        "job_url": job.job_url,
        "title": job.title,
        "company": job.company,
        "company_url": job.company_url,
        "company_logo_url": getattr(job, "company_logo_url", None),
        "location_text": job.location_text,
        "remote_status": job.remote_status,
        "date_posted": _fmt_dt(job.date_posted),
        "date_posted_text": getattr(job, "date_posted_text", None) or "",
        "compensation": {
            "interval": c.get("interval"),
            "min_amount": c.get("min_amount"),
            "max_amount": c.get("max_amount"),
            "currency": c.get("currency"),
            "min_annual_usd": c.get("min_annual_usd"),
            "max_annual_usd": c.get("max_annual_usd"),
            "min_hourly_usd": c.get("min_hourly_usd"),
            "max_hourly_usd": c.get("max_hourly_usd"),
        },
        "already_applied": job.already_applied,
        "applied_text": job.applied_text,
        "prefilter_pass": job.prefilter_pass,
        "prefilter_reasons": list(job.prefilter_reasons or []),
        # AI highlights (intentionally not including raw ai_output JSON)
        "ai_verdict": job.ai_verdict,
        "ai_kill_criteria": job.ai_kill_criteria,
        "ai_summary": job.ai_summary,
    }

    ai_facts = _sanitize_ai_job_facts(ai_output) if ai_output else None
    if ai_facts:
        # Email quality-of-life: if scrape missed basics, fall back to AI-extracted facts.
        title_fallback = ai_facts.get("title")
        company_fallback = ai_facts.get("company") or ai_facts.get("employer")
        location_fallback = ai_facts.get("location") or ai_facts.get("location_text")
        if (not base.get("title")) or str(base.get("title")).strip() in ("", "N/A"):
            if isinstance(title_fallback, str) and title_fallback.strip():
                base["title"] = title_fallback.strip()
        if (not base.get("company")) or str(base.get("company")).strip() in ("", "N/A"):
            if isinstance(company_fallback, str) and company_fallback.strip():
                base["company"] = company_fallback.strip()
        loc = str(base.get("location_text") or "").strip()
        if not loc:
            if isinstance(location_fallback, str) and location_fallback.strip():
                clean = _extract_clean_location(location_fallback)
                if clean:
                    base["location_text"] = clean
                elif len(location_fallback.strip()) < 80:
                    base["location_text"] = location_fallback.strip()
        elif "Skip to main content" in loc or "My Network" in loc:
            clean = _extract_clean_location(loc)
            if clean:
                base["location_text"] = clean

        base["ai_job_facts_extracted"] = ai_facts

    core_mission = _extract_ai_core_mission(ai_output) if ai_output else None
    if core_mission:
        base["ai_core_mission"] = core_mission

    # Flag when AI hit token cap (response truncated); only set when fallback parser recorded it.
    if ai_output and isinstance(ai_output, dict):
        parsed = ai_output.get("parsed") if isinstance(ai_output.get("parsed"), dict) else ai_output
        meta = parsed.get("_meta") if isinstance(parsed.get("_meta"), dict) else {}
        base["ai_output_truncated"] = bool(meta.get("truncated"))

    # Always sanitize location_text when it contains page header/nav junk (from scraper or AI).
    loc = str(base.get("location_text") or "").strip()
    if loc and ("Skip to main content" in loc or "My Network" in loc or "Advertise" in loc):
        clean = _extract_clean_location(loc)
        if clean:
            base["location_text"] = clean

    return base


def _dedupe_title_phrase(title: str) -> str:
    """
    If the title looks like "Foo Foo" or "Foo Bar Foo Bar" (repeated phrase), return a single copy.
    Fixes duplication from scraper (e.g. card link + detail panel or DOM quirks).
    """
    s = (title or "").strip()
    if not s or len(s) < 3:
        return s
    words = s.split()
    if len(words) < 2:
        return s
    # Check if first N words repeat: words[0:N] == words[N:2*N]
    for n in range(1, (len(words) // 2) + 1):
        if words[:n] == words[n : n * 2]:
            return " ".join(words[:n])
    return s


def _escape(v: Any) -> str:
    if v is None:
        return ""
    return html.escape(str(v))


def _to_bullet_list(text: str, *, delimiters: tuple[str, ...] = ("; ", " | ")) -> str:
    """
    Split text by semicolon or pipe delimiters and render as HTML bullet list.
    Tries delimiters in order; first match wins.
    """
    if not text or not text.strip():
        return ""
    s = text.strip()
    parts: list[str] = []
    for sep in delimiters:
        if sep in s:
            parts = [p.strip() for p in s.split(sep) if p.strip()]
            break
    if not parts:
        parts = [s]
    items = "".join(
        f"<li style=\"margin:4px 0;\">{_escape(p)}</li>" for p in parts
    )
    return f"<ul style=\"margin:4px 0 4px 20px;padding:0;list-style-type:disc;\">{items}</ul>"


def _company_initials(name: Any) -> str:
    s = " ".join(str(name or "").strip().split())
    if not s or s.lower() == "n/a":
        return "?"
    parts = [p for p in s.replace("&", " ").split(" ") if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][:1] + parts[1][:1]).upper()


def _badge(*, text: str, bg: str, fg: str, border: str) -> str:
    t = _escape(text)
    return (
        f"<span style=\"display:inline-block;padding:3px 8px;border-radius:999px;"
        f"background:{bg};color:{fg};border:1px solid {border};font-size:12px;line-height:16px;\">{t}</span>"
    )


def _pill(*, icon: str, text: str) -> str:
    return (
        "<span style=\"display:inline-block;margin-right:6px;margin-top:6px;"
        "padding:5px 10px;border-radius:999px;border:1px solid #e5e7eb;background:#f9fafb;"
        "font-size:12px;line-height:16px;color:#111827;\">"
        f"<span style=\"font-weight:700;margin-right:6px;\">{_escape(icon)}</span>{_escape(text)}"
        "</span>"
    )


def _render_job_card(*, md: dict[str, Any]) -> str:
    title = _dedupe_title_phrase(str(md.get("title") or "").strip())
    company = str(md.get("company") or "").strip()
    job_url = str(md.get("job_url") or "").strip()
    company_url = str(md.get("company_url") or "").strip()
    logo = str(md.get("company_logo_url") or "").strip()
    loc = str(md.get("location_text") or "").strip()
    remote = str(md.get("remote_status") or "").strip()
    posted_raw = str(md.get("date_posted_text") or "").strip()
    # Flag when parsed date is missing so user can review (max_age_days may not have been enforced).
    has_parsed_date = bool(md.get("date_posted"))
    if not has_parsed_date:
        posted = (posted_raw + " — review").strip() if posted_raw else "Date unknown — review"
    else:
        posted = posted_raw
    verdict = str(md.get("ai_verdict") or "").strip().upper()
    core_mission = str(md.get("ai_core_mission") or "").strip()
    summary = str(md.get("ai_summary") or "").strip()
    kill = str(md.get("ai_kill_criteria") or "").strip()

    comp_pretty = ""
    comp = md.get("compensation")
    if isinstance(comp, dict):
        comp_pretty = _fmt_compensation_short(comp)

    verdict_badge = ""
    if verdict == "YES":
        verdict_badge = _badge(text="YES", bg="#ecfdf5", fg="#047857", border="#a7f3d0")
    elif verdict == "MAYBE":
        verdict_badge = _badge(text="MAYBE", bg="#fffbeb", fg="#b45309", border="#fde68a")
    elif verdict:
        verdict_badge = _badge(text=verdict, bg="#fef2f2", fg="#b91c1c", border="#fecaca")

    truncated_badge = ""
    if md.get("ai_output_truncated"):
        truncated_badge = _badge(text="Token limit", bg="#fef3c7", fg="#92400e", border="#fcd34d")

    badges_html = " ".join(b for b in (verdict_badge, truncated_badge) if b)

    # Logo: real image if available; otherwise initials placeholder.
    if logo:
        logo_html = (
            f"<img src=\"{_escape(logo)}\" width=\"44\" height=\"44\" "
            "style=\"display:block;border-radius:10px;border:1px solid #e5e7eb;object-fit:cover;\" "
            f"alt=\"{_escape(company)}\" /><br /> {badges_html}"
        )
    else:
        logo_html = (
            "<div style=\"width:44px;height:44px;border-radius:10px;border:1px solid #e5e7eb;"
            "background:#f3f4f6;color:#111827;font-weight:800;font-size:14px;line-height:44px;"
            f"text-align:center;\">{_escape(_company_initials(company))}</div><br /> {badges_html}"
        )

    title_html = _escape(title or "Untitled role")
    if job_url:
        title_html = f"<a href=\"{_escape(job_url)}\" style=\"color:#111827;\">{title_html}</a>"

    company_html = _escape(company or "Unknown company")
    if company_url:
        company_html = f"<a href=\"{_escape(company_url)}\" style=\"color:#4b5563;text-decoration:none;\">{company_html}</a>"

    pills = ""
    if comp_pretty:
        pills += _pill(icon="$", text=comp_pretty)
    if loc:
        pills += _pill(icon="LOC", text=loc)
    if remote and remote != "unknown":
        pills += _pill(icon="MODE", text=remote)
    if posted:
        pills += _pill(icon="POSTED", text=posted)

    # Secondary text (keep compact)
    extra = ""
    if core_mission:
        extra += (
            "<div style=\"margin-top:10px;color:#111827;font-size:13px;line-height:18px;\">"
            "<strong style=\"color:#111827;\">Role Summary:</strong> "
            f"<span style=\"color:#374151;\">{_escape(core_mission)}</span>"
            "</div>"
        )
    if summary:
        summary_bullets = _to_bullet_list(summary)
        extra += (
            "<div style=\"margin-top:10px;color:#374151;font-size:13px;line-height:18px;\">"
            f"<strong style=\"color:#111827;\">Verdict:</strong> {summary_bullets}"
            "</div>"
        )
    if kill:
        kill_bullets = _to_bullet_list(kill, delimiters=(" | ", "; "))
        extra += (
            "<div style=\"margin-top:8px;color:#6b7280;font-size:12px;line-height:16px;\">"
            f"<strong style=\"color:#374151;\">kill_criteria</strong>: {kill_bullets}"
            "</div>"
        )

    return (
        "<div style=\"border:1px solid #e5e7eb;border-radius:14px;padding:14px;margin:14px 0;\">"
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" style=\"border-collapse:collapse;width:100%;\">"
        "<tr>"
        f"<td valign=\"top\" style=\"width:52px;padding-right:10px;\">{logo_html}</td>"
        "<td valign=\"top\">"
        f"<div style=\"font-size:16px;line-height:20px;font-weight:800;margin:0 0 2px 0;\">{title_html}, <span style=\"font-size:13px;line-height:18px;color:#4b5563;\">{company_html}</span></div>"
        f"<div>{pills}</div>"
        f"{extra}"
        "</td>"
        "</tr>"
        "</table>"
        "</div>"
    )


def _build_email_html(
    *,
    run_meta: dict[str, Any],
    jobs: list[JobPost],
    ai_outputs_by_job_id: dict[str, dict[str, Any]] | None = None,
) -> str:
    # Run summary
    summary_rows = []
    for k in [
        "run_at_utc",
        "collections_selected",
        "scraped_count",
        "passed_prefilter_count",
        "ai_evaluated_count",
        "new_ai_evaluated_count",
        "max_ai_evals_per_run",
        "top_applicant_url",
        "single_job_url",
        "xlsx_path",
        "csv_path",
    ]:
        if k in run_meta and run_meta.get(k) is not None:
            summary_rows.append((k, run_meta.get(k)))

    # Jobs (render as compact cards)
    job_cards = []
    for j in jobs:
        md = _job_metadata_dict(j, ai_output=(ai_outputs_by_job_id or {}).get(j.job_id))
        job_cards.append(_render_job_card(md=md))

    summary_html = "".join(
        f"<tr><td style=\"padding:6px 10px;border-bottom:1px solid #eee;\"><strong>{_escape(k)}</strong></td>"
        f"<td style=\"padding:6px 10px;border-bottom:1px solid #eee;\">{_escape(v)}</td></tr>"
        for k, v in summary_rows
    )

    return f"""
<!doctype html>
<html>
  <body style="font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; padding: 10px;">

    <!-- <h3 style="margin:20px 0 8px 0;">Run summary</h3>
    <table style="border-collapse:collapse; width:100%; max-width:900px; border:1px solid #eee;">
      {summary_html}
    </table> -->

    <h3 style="margin:20px 0 8px 0;">{len(jobs)} New AI-evaluated jobs</h3>
    {''.join(job_cards)}
  </body>
</html>
""".strip()


def send_new_ai_results_email(
    *,
    notifications: NotificationsConfig,
    run_meta: dict[str, Any],
    new_ai_jobs: list[JobPost],
    ai_outputs_by_job_id: dict[str, dict[str, Any]] | None = None,
) -> None:
    """
    Send an email only if there are new AI-evaluated jobs in this run.

    Uses Resend when notifications.provider == "resend".
    """
    if not notifications.enabled:
        return
    if not new_ai_jobs:
        return

    provider = (notifications.provider or "").strip().lower()
    if provider != "resend":
        raise ValueError(f"Unsupported notifications.provider: {notifications.provider!r}")

    # Prefer explicit config, but allow env var override.
    api_key = (notifications.resend_api_key or "").strip() or (os.environ.get("RESEND_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError(
            "Notifications enabled but missing Resend API key. "
            "Set notifications.resend_api_key in config.yaml or RESEND_API_KEY in the environment."
        )

    from_email = (notifications.from_email or "").strip()
    if not from_email:
        raise RuntimeError("Notifications enabled but missing notifications.from_email in config.yaml.")

    to_emails = [e.strip() for e in (notifications.to_emails or []) if e and str(e).strip()]
    if not to_emails:
        raise RuntimeError("Notifications enabled but missing notifications.to_emails (or notifications.to) in config.yaml.")

    # Subject templating: allow {new_ai_count} + any run_meta keys.
    fmt_ctx = {"new_ai_count": len(new_ai_jobs), **(run_meta or {})}
    try:
        subject = (notifications.subject or "").format(**fmt_ctx).strip() or "LinkedIn job eval"
    except Exception:
        subject = notifications.subject or "LinkedIn job eval"

    html_body = _build_email_html(
        run_meta=run_meta or {},
        jobs=new_ai_jobs,
        ai_outputs_by_job_id=ai_outputs_by_job_id or None,
    )
    text_body = f"New AI-evaluated jobs: {len(new_ai_jobs)}"

    try:
        import resend  # type: ignore
    except ModuleNotFoundError as e:
        raise RuntimeError(
            "Resend SDK not installed. Install it in your venv:\n"
            "  pip install resend\n"
            "Then re-run."
        ) from e

    resend.api_key = api_key
    payload: dict[str, Any] = {
        "from": from_email,
        "to": to_emails[0] if len(to_emails) == 1 else to_emails,
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }

    log.info("Sending notification email via Resend to %s recipient(s)", len(to_emails))
    resend.Emails.send(payload)

