from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from siftr.config import load_config
from siftr.export_csv import export_jobs_csv
from siftr.export_xlsx import export_jobs_xlsx
from siftr.prefilter import apply_prefilters
from siftr.scrape_linkedin_top_applicant import ScrapeResult, scrape_job_url, scrape_top_applicant
from siftr.seen_cache import SeenJobsCache
from siftr.util import configure_logging, dump_json, ensure_dir


def _resolve_output_path(
    path: str | None,
    *,
    run_dt_utc: datetime,
    stamp: str,
    datestamp: bool,
) -> str | None:
    if not path:
        return None

    # Placeholder support for full control.
    # Example: "out/jobs.{stamp}.csv"
    if "{stamp}" in path:
        return path.replace("{stamp}", stamp)

    # Automatic datestamp insertion (before extension).
    if datestamp:
        p = Path(path)
        return str(p.with_name(f"{p.stem}.{stamp}{p.suffix}"))

    return path


def _available_collections(cfg) -> dict[str, str]:
    run = cfg.run
    col: dict[str, str] = {}
    if isinstance(getattr(run, "collections", None), dict):
        for k, v in (run.collections or {}).items():
            if isinstance(k, str) and isinstance(v, str) and v.strip():
                col[k.strip()] = v.strip()
    # Back-compat convenience keys
    if getattr(run, "top_applicant_url", None):
        col.setdefault("top_applicant", str(run.top_applicant_url).strip())
    if getattr(run, "recommended_url", None):
        col.setdefault("recommended", str(run.recommended_url).strip())
    # drop empties
    return {k: v for k, v in col.items() if v}


def _parse_collections_arg(values: list[str]) -> list[str]:
    names: list[str] = []
    for v in values:
        for part in (v or "").split(","):
            p = part.strip()
            if p:
                names.append(p)
    return _dedupe_preserve_order(names)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for n in items:
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _choose_collections_interactive(options: list[str]) -> list[str]:
    print("\nAvailable collections:")
    for i, name in enumerate(options, start=1):
        print(f"  {i}) {name}")
    raw = input("\nSelect collections (comma-separated numbers, or Enter for ALL): ").strip()
    if not raw:
        return options
    picks: list[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            idx = int(part)
        except ValueError:
            continue
        if 1 <= idx <= len(options):
            picks.append(options[idx - 1])
    out = _dedupe_preserve_order(picks)
    return out or options


def _attach_ai_fields(*, job, output: object) -> None:
    """
    Populate JobPost AI fields in a human-friendly way:
    - ai_verdict: YES/MAYBE/NO when present
    - ai_kill_criteria: joined string when present
    - ai_summary: short glanceable summary
    - ai_output: pretty JSON (or excerpt for raw text)
    """
    if isinstance(output, dict):
        # Prefer direct keys; fall back to parsed recovery if present.
        parsed = output.get("parsed") if isinstance(output.get("parsed"), dict) else None
        src = parsed or output

        verdict = output.get("verdict")
        if not isinstance(verdict, str) and parsed:
            verdict = parsed.get("verdict")
        if isinstance(verdict, str):
            job.ai_verdict = verdict.strip().upper()

        kc = output.get("kill_criteria")
        if not isinstance(kc, list) and parsed:
            kc = parsed.get("kill_criteria")
        if isinstance(kc, list):
            items = [str(x).strip() for x in kc if str(x).strip()]
            job.ai_kill_criteria = " | ".join(items) if items else None

        # Prefer a concise summary line.
        if isinstance(job.ai_verdict, str) or isinstance(job.ai_kill_criteria, str):
            bits: list[str] = []
            if isinstance(job.ai_verdict, str):
                bits.append(job.ai_verdict)
            if isinstance(kc, list) and kc:
                take = [str(x).strip() for x in kc[:2] if str(x).strip()]
                if take:
                    bits.append("; ".join(take))
            # If we only have a verdict (no kill criteria), try to add one more useful
            # short highlight from the structured output (so email shows verdict reasoning).
            if len(bits) == 1:
                extra: str | None = None
                # For YES/MAYBE, prefer reasoning from if_apply_anyway (prompt asks AI to explain why there).
                if (job.ai_verdict or "").strip().upper() in ("YES", "MAYBE"):
                    try:
                        iaa = src.get("if_apply_anyway") if isinstance(src.get("if_apply_anyway"), dict) else None
                        if iaa:
                            for key in ("summary", "reason", "why", "one_liner", "rationale"):
                                val = iaa.get(key)
                                if isinstance(val, str) and val.strip():
                                    extra = " ".join(val.split())
                                    break
                            if not extra and isinstance(iaa.get("reasons"), list):
                                parts = [str(x).strip() for x in iaa["reasons"][:2] if str(x).strip()]
                                if parts:
                                    extra = " ".join(" ".join(p.split()) for p in parts)
                            if not extra:
                                for _, v in iaa.items():
                                    if isinstance(v, str) and len(v.strip()) > 15:
                                        extra = " ".join(v.split())
                                        break
                    except Exception:
                        pass
                if not extra:
                    try:
                        fevr = src.get("fit_exposure_vs_resume") if isinstance(src.get("fit_exposure_vs_resume"), dict) else None
                        if fevr and isinstance(fevr.get("asymmetry"), str):
                            extra = fevr.get("asymmetry")
                    except Exception:
                        extra = None
                if not extra:
                    try:
                        jfe = src.get("job_facts_extracted") if isinstance(src.get("job_facts_extracted"), dict) else None
                        if jfe and isinstance(jfe.get("critical_gap"), str):
                            extra = jfe.get("critical_gap")
                    except Exception:
                        extra = None
                if extra:
                    extra = " ".join(str(extra).split())
                    bits.append(extra)
            job.ai_summary = " — ".join(bits) if bits else None
        else:
            raw_text = output.get("raw_text") if isinstance(output.get("raw_text"), str) else None
            if raw_text:
                job.ai_summary = (raw_text[:600] + "…") if len(raw_text) > 600 else raw_text
        # Ensure at least verdict appears as summary when we have it (so email always shows Verdict line).
        if (job.ai_verdict or "").strip() and not (job.ai_summary or "").strip():
            job.ai_summary = (job.ai_verdict or "").strip()

        # Store pretty JSON (readable in cache/export).
        try:
            job.ai_output = json.dumps(output, ensure_ascii=False, indent=2, sort_keys=False)
        except Exception:
            job.ai_output = str(output)
        return

    if isinstance(output, str):
        job.ai_output = output
        job.ai_summary = (output[:600] + "…") if len(output) > 600 else output
        return

    # Unknown type
    job.ai_output = str(output)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="Path to config.yaml")
    ap.add_argument(
        "--job-url",
        help="Analyze a single LinkedIn job posting URL (bypasses Top Applicant collection scrape).",
    )
    ap.add_argument(
        "--respect-prefilter",
        action="store_true",
        help="In --job-url mode, only run AI if the job passes prefilters.",
    )
    ap.add_argument(
        "--collections",
        action="append",
        default=[],
        help="Comma-separated collection names to scrape (e.g. top_applicant,recommended). Can be provided multiple times.",
    )
    ap.add_argument(
        "--choose-collections",
        action="store_true",
        help="Interactively choose which collections to scrape (multi-select).",
    )
    ap.add_argument(
        "--verbose",
        action="store_true",
        help="Enable slightly more console output (still INFO-level).",
    )
    ap.add_argument(
        "--login",
        action="store_true",
        help="Run with a visible browser and pause for manual LinkedIn login.",
    )
    args = ap.parse_args()

    configure_logging(verbose=bool(args.verbose))
    log = logging.getLogger("siftr")

    cfg = load_config(Path(args.config).resolve())
    out_dir = ensure_dir(Path(cfg.run.out_dir))
    log.info("Loaded config from %s", args.config)
    log.info("Output dir: %s", out_dir)

    seen_cache: SeenJobsCache | None = None
    # Snapshot of cache state at run start (used for logging and to distinguish prior scans vs this-run scans).
    cache_scanned_at_before: dict[str, str] = {}
    try:
        if getattr(cfg.scrape, "seen_cache", None) and bool(getattr(cfg.scrape.seen_cache, "enabled", False)):
            seen_cache = SeenJobsCache(cfg=cfg.scrape.seen_cache, out_dir=out_dir)
            log.info(
                "Seen cache enabled (skip_if_scanned_within_days=%s, path=%s)",
                int(getattr(cfg.scrape.seen_cache, "skip_if_scanned_within_days", 0)),
                str(seen_cache.path),
            )
            # Build a small snapshot map for later decisions/logging.
            try:
                for jid, rec in getattr(seen_cache, "_data", {}).items():
                    if isinstance(jid, str) and isinstance(rec, dict) and isinstance(rec.get("last_scanned_at_utc"), str):
                        cache_scanned_at_before[jid] = rec["last_scanned_at_utc"]
            except Exception:
                cache_scanned_at_before = {}
        else:
            log.info("Seen cache disabled")
    except Exception:
        log.exception("Failed to initialize seen cache; continuing without it.")
        seen_cache = None

    run_dt_utc = datetime.now(timezone.utc)
    stamp = run_dt_utc.strftime(getattr(cfg.export, "datestamp_format", "%Y%m%d-%H%M%S"))
    xlsx_path = _resolve_output_path(
        cfg.export.xlsx_path,
        run_dt_utc=run_dt_utc,
        stamp=stamp,
        datestamp=bool(getattr(cfg.export, "datestamp", False)),
    )
    csv_path = _resolve_output_path(
        cfg.export.csv_path,
        run_dt_utc=run_dt_utc,
        stamp=stamp,
        datestamp=bool(getattr(cfg.export, "datestamp", False)),
    )

    # Fail fast on missing Anthropic key (before Playwright scrape).
    # Note: if you set max_ai_evals_per_run: 0, we treat that as "scrape only"
    # and do not require an API key.
    if cfg.ai.provider == "anthropic" and int(cfg.run.max_ai_evals_per_run) > 0:
        # Import lazily so scrape-only can run without AI deps.
        try:
            from siftr.ai_eval_anthropic import evaluate_jobs_anthropic  # noqa: F401
        except ModuleNotFoundError as e:
            raise RuntimeError(
                "Anthropic SDK not installed in this Python environment.\n\n"
                "You're likely not running inside the project virtualenv. Activate it, then install deps:\n"
                "  .\\.venv\\Scripts\\Activate.ps1\n"
                "  pip install -r requirements.txt\n"
            ) from e

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "Missing ANTHROPIC_API_KEY environment variable.\n\n"
                "If you set it in Windows 'User'/'System' variables, restart your terminal (and Cursor) "
                "so the new environment is inherited. Or set it for this session in PowerShell:\n"
                '  $env:ANTHROPIC_API_KEY = "your-key-here"\n\n'
                "Tip: set run.max_ai_evals_per_run: 0 to run scrape/export without AI."
            )

    log.info(
        "Starting scrape (login=%s, headless=%s, max_jobs=%s)",
        bool(args.login),
        bool(cfg.scrape.browser.headless) if not args.login else False,
        int(cfg.scrape.max_jobs),
    )
    if args.job_url:
        log.info("Single-job mode: %s", args.job_url)
        job = scrape_job_url(cfg, job_url=str(args.job_url), login=bool(args.login), seen_cache=seen_cache)
        scraped = [job]
        log.info("Scraped 1 job (job_id=%s)", job.job_id)
    else:
        available = _available_collections(cfg)
        if not available:
            raise RuntimeError("No collection URLs configured. Add run.top_applicant_url or run.collections in config.yaml.")

        requested = _parse_collections_arg(list(args.collections or []))
        option_names = sorted(available.keys())

        if args.choose_collections:
            selected_names = _choose_collections_interactive(option_names)
        elif requested:
            missing = [n for n in requested if n not in available]
            if missing:
                raise RuntimeError(
                    f"Unknown collection(s): {missing}. Available: {sorted(available.keys())}"
                )
            selected_names = requested
        else:
            # Default: scrape ALL configured collections unless user specifies otherwise.
            selected_names = option_names
            if len(option_names) > 1:
                log.info(
                    "Multiple collections configured; defaulting to ALL collections. "
                    "Use --choose-collections or --collections to override."
                )

        log.info("Scraping collections: %s", ", ".join(selected_names))

        by_id: dict[str, object] = {}
        scraped = []
        total_cards_found = 0
        total_skipped_seen = 0
        total_extracted_across_collections = 0
        collection_stats: dict[str, dict] = {}
        for name in selected_names:
            url = available[name]
            log.info("Scraping collection '%s': %s", name, url)
            res: ScrapeResult = scrape_top_applicant(
                cfg,
                login=bool(args.login),
                collection_url=url,
                collection_name=name,
                seen_cache=seen_cache,
            )
            total_cards_found += int(res.cards_found or 0)
            total_skipped_seen += int(res.skipped_seen_cache_count or 0)
            total_extracted_across_collections += int(res.extracted_count or 0)
            collection_stats[name] = {
                "collection_name": name,
                "collection_url": url,
                "cards_found": int(res.cards_found or 0),
                "extracted_count": int(res.extracted_count or 0),
                "skipped_seen_cache_count": int(res.skipped_seen_cache_count or 0),
            }
            for j in res.jobs:
                if j.job_id not in by_id:
                    by_id[j.job_id] = True
                    scraped.append(j)
        log.info("Scraped %s unique jobs across %s collection(s)", len(scraped), len(selected_names))
        if total_skipped_seen:
            log.info("Seen-cache skipped %s job(s) across collections", total_skipped_seen)

    passed = []
    for j in scraped:
        ok, reasons = apply_prefilters(j, cfg.filters)
        j.prefilter_pass = ok
        j.prefilter_reasons = reasons
        if seen_cache and j.job_id:
            seen_cache.update_prefilter(
                job_id=j.job_id,
                run_at_utc=run_dt_utc,
                passed=bool(ok),
                reasons=list(reasons or []),
            )
        # One line per job for quick log scanning (post-seen-cache).
        title_short = ((j.title or "?")[:50] + "…") if (j.title and len(j.title) > 50) else (j.title or "?")
        company_short = ((j.company or "?")[:30] + "…") if (j.company and len(j.company) > 30) else (j.company or "?")
        outcome = "PASS" if ok else "REJECT"
        reason_str = (" " + "; ".join(reasons)) if (reasons and not ok) else ""
        log.info(
            "Prefilter: job_id=%s title=%s company=%s -> %s%s",
            j.job_id,
            title_short,
            company_short,
            outcome,
            reason_str,
        )
        if ok:
            passed.append(j)
    log.info("Prefilter passed: %s / %s", len(passed), len(scraped))

    # AI eval only for first N passed jobs each run
    if args.job_url:
        # In single-job mode, default is to always evaluate (even if it fails prefilters)
        # so you can iterate on the AI analysis without fighting filter logic.
        if bool(args.respect_prefilter):
            to_eval = passed[:1] if int(cfg.run.max_ai_evals_per_run) > 0 else []
        else:
            to_eval = scraped[:1] if int(cfg.run.max_ai_evals_per_run) > 0 else []
    else:
        to_eval = passed[: int(cfg.run.max_ai_evals_per_run)]
    ai_records = []
    if to_eval:
        log.info("Running AI eval on %s jobs (model=%s)", len(to_eval), cfg.ai.model)
        # Import lazily so we don't require Anthropic deps for scrape-only.
        from siftr.ai_eval_anthropic import evaluate_jobs_anthropic

        ai_records = evaluate_jobs_anthropic(ai_cfg=cfg.ai, jobs=to_eval, out_dir=out_dir)
        log.info("AI eval complete (%s records)", len(ai_records))
    else:
        log.info("Skipping AI eval (0 jobs selected; max_ai_evals_per_run=%s)", int(cfg.run.max_ai_evals_per_run))

    # attach AI output back to jobs
    by_id = {r["job_id"]: r for r in ai_records}
    for j in to_eval:
        rec = by_id.get(j.job_id) or {}
        output = rec.get("output") or {}
        _attach_ai_fields(job=j, output=output)

    # persist AI summary into seen cache (verdict + kill criteria + timestamp)
    if seen_cache and ai_records:
        for r in ai_records:
            if not isinstance(r, dict):
                continue
            jid = r.get("job_id")
            if not isinstance(jid, str) or not jid:
                continue
            seen_cache.update_ai(
                job_id=jid,
                run_at_utc=run_dt_utc,
                model=str(r.get("model")) if r.get("model") else None,
                output=(r.get("output") if isinstance(r.get("output"), dict) else None),
                cache_hit=bool(r.get("_cache_hit", False)),
                input_hash=(str(r.get("input_hash")) if r.get("input_hash") else None),
            )

    # Determine which AI records were newly evaluated this run (exclude cache hits).
    new_ai_ids: list[str] = []
    for r in ai_records or []:
        try:
            if not bool(r.get("_cache_hit", False)) and isinstance(r.get("job_id"), str):
                new_ai_ids.append(r["job_id"])
        except Exception:
            continue
    new_ai_ids = _dedupe_preserve_order(new_ai_ids)

    run_meta = {
        "run_at_utc": run_dt_utc.isoformat(),
        "top_applicant_url": getattr(cfg.run, "top_applicant_url", None),
        "recommended_url": getattr(cfg.run, "recommended_url", None),
        "single_job_url": str(args.job_url) if args.job_url else None,
        "collections_selected": _parse_collections_arg(list(args.collections or [])) if args.collections else None,
        # Back-compat: scraped_count is the number of UNIQUE extracted jobs (post-skip + post-dedupe).
        "scraped_count": len(scraped),
        # Additional scrape counters (more explicit for debugging scheduled runs):
        "scrape_cards_found_total": int(total_cards_found) if not args.job_url else None,
        "scrape_extracted_total": int(total_extracted_across_collections) if not args.job_url else None,
        "scrape_unique_extracted_total": int(len(scraped)),
        "scrape_skipped_seen_cache_total": int(total_skipped_seen) if not args.job_url else None,
        "collections_used": {k: available[k] for k in selected_names} if not args.job_url else None,
        "passed_prefilter_count": len(passed),
        "ai_evaluated_count": len(to_eval),
        "new_ai_evaluated_count": len(new_ai_ids),
        "max_ai_evals_per_run": int(cfg.run.max_ai_evals_per_run),
        "seen_cache_enabled": bool(seen_cache.enabled) if seen_cache else False,
        "seen_cache_skip_if_scanned_within_days": int(getattr(cfg.scrape.seen_cache, "skip_if_scanned_within_days", 0))
        if getattr(cfg.scrape, "seen_cache", None)
        else None,
        "seen_cache_skipped_seen_count": int(total_skipped_seen) if not args.job_url else None,
        "xlsx_path": str(xlsx_path) if xlsx_path else None,
        "csv_path": str(csv_path) if csv_path else None,
    }

    # Track emailed jobs for run logging (populated below if notifications send).
    emailed_job_ids: list[str] = []

    export_jobs_xlsx(
        xlsx_path=xlsx_path,
        all_jobs=scraped,
        passed_jobs=to_eval,
        run_meta=run_meta,
    ) if xlsx_path else None

    if xlsx_path:
        log.info("Wrote %s", xlsx_path)

    if csv_path:
        written = export_jobs_csv(
            csv_path=csv_path,
            all_jobs=scraped,
            passed_jobs=to_eval,
            run_meta=run_meta,
        )
        for p in written:
            log.info("Wrote %s", p)

    # Notifications: send only if this run produced new AI evaluations.
    if getattr(cfg, "notifications", None) and bool(getattr(cfg.notifications, "enabled", False)):
        if new_ai_ids:
            try:
                from siftr.notifications import send_new_ai_results_email

                jobs_by_id = {j.job_id: j for j in to_eval}
                new_jobs = [jobs_by_id[jid] for jid in new_ai_ids if jid in jobs_by_id]
                if not args.job_url:
                    # Bulk mode: only notify on actionable YES/MAYBE.
                    new_jobs = [j for j in new_jobs if (j.ai_verdict or "").strip().upper() in ("YES", "MAYBE")]
                # Single-job mode: always email (honest feedback even for NO).
                emailed_job_ids = [j.job_id for j in new_jobs if isinstance(j.job_id, str)]
                ai_outputs_by_id = {
                    str(r.get("job_id")): (r.get("output") or {})
                    for r in (ai_records or [])
                    if isinstance(r, dict)
                    and (not bool(r.get("_cache_hit", False)))
                    and isinstance(r.get("job_id"), str)
                    and isinstance(r.get("output"), dict)
                }
                send_new_ai_results_email(
                    notifications=cfg.notifications,
                    run_meta=run_meta,
                    new_ai_jobs=new_jobs,
                    ai_outputs_by_job_id=ai_outputs_by_id,
                )
                log.info("Notification email sent (%s new AI jobs).", len(new_jobs))
            except Exception:
                log.exception("Failed to send notification email.")
                raise
        else:
            log.info("No new AI evaluations this run; skipping notifications.")

    # Per-run JSON log for scheduled/overnight debugging.
    # This is intentionally verbose but structured: scrape -> prefilter -> AI -> email.
    try:
        logs_dir = ensure_dir(out_dir / "logs")
        run_log_path = logs_dir / f"run.{stamp}.json"

        ai_by_id: dict[str, dict] = {str(r.get("job_id")): r for r in (ai_records or []) if isinstance(r, dict) and r.get("job_id")}
        to_eval_ids = {j.job_id for j in (to_eval or []) if isinstance(getattr(j, "job_id", None), str)}
        emailed_set = set(emailed_job_ids or [])
        run_log = {
            "run_meta": run_meta,
            "collections_resolved": selected_names if not args.job_url else ["single_job"],
            "collections": list(collection_stats.values()) if not args.job_url else None,
            "scraped_jobs": [
                {
                    "job_id": j.job_id,
                    "job_url": j.job_url,
                    "title": j.title,
                    "company": j.company,
                    "location_text": j.location_text,
                    "remote_status": j.remote_status,
                    "date_posted": j.date_posted.isoformat() if j.date_posted else None,
                    "date_posted_text": getattr(j, "date_posted_text", None),
                    "already_applied": j.already_applied,
                    "applied_text": j.applied_text,
                    "seen_cache_last_scanned_at_before": cache_scanned_at_before.get(j.job_id),
                }
                for j in scraped
            ],
            "prefilter": [
                {
                    "job_id": j.job_id,
                    "passed": bool(j.prefilter_pass),
                    "reasons": list(j.prefilter_reasons or []),
                }
                for j in scraped
            ],
            "ai": [
                {
                    "job_id": j.job_id,
                    "selected_for_ai": j.job_id in to_eval_ids,
                    "ai_verdict": j.ai_verdict,
                    "ai_kill_criteria": j.ai_kill_criteria,
                    "ai_cache_hit": bool(ai_by_id.get(j.job_id, {}).get("_cache_hit", False)) if j.job_id in to_eval_ids else None,
                    "ai_input_hash": ai_by_id.get(j.job_id, {}).get("input_hash") if j.job_id in to_eval_ids else None,
                }
                for j in scraped
            ],
            "emailed_jobs": [
                {
                    "job_id": j.job_id,
                    "job_url": j.job_url,
                    "title": j.title,
                    "company": j.company,
                    "date_posted_text": getattr(j, "date_posted_text", None),
                    "ai_verdict": j.ai_verdict,
                    "ai_kill_criteria": j.ai_kill_criteria,
                }
                for j in to_eval
                if j.job_id in emailed_set
            ],
        }
        dump_json(run_log_path, run_log)
        log.info("Wrote run log %s", run_log_path)
    except Exception:
        log.exception("Failed to write per-run JSON log.")

    if seen_cache:
        try:
            seen_cache.save()
        except Exception:
            log.exception("Failed to write seen cache to disk.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

