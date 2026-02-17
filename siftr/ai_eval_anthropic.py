from __future__ import annotations

import logging
import json
import os
import textwrap
from dataclasses import asdict
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from siftr.config import AIConfig
from siftr.models import JobPost
from siftr.raw_text_parser import parse_raw_text_to_parsed
from siftr.util import dump_json, ensure_dir, sha256_text

log = logging.getLogger(__name__)


def _parse_json_best_effort(text: str) -> dict[str, Any] | None:
    """
    Try hard to parse JSON even if the model wraps it with extra text.
    Returns a dict if successful, else None.
    """
    if not text:
        return None
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except Exception:
        pass

    # Common failure mode: leading/trailing commentary. Attempt to extract the
    # outermost JSON object substring.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            v = json.loads(text[start : end + 1])
            return v if isinstance(v, dict) else None
        except Exception:
            return None
    return None


def _build_prompt(*, resume_text: str, job: JobPost) -> str:
    facts = {
        "job_id": job.job_id,
        "job_url": job.job_url,
        "title": job.title,
        "company": job.company,
        "location_text": job.location_text,
        "remote_status": job.remote_status,
        "date_posted": job.date_posted.isoformat() if job.date_posted else None,
        "compensation": asdict(job.compensation),
    }
    return textwrap.dedent(
        f"""
        You are a deeply practical, highly critical career evaluator—an incorrigible realist.
        Your job is not encouragement. Your job is to determine where the candidate could actually excel by aggressively stress-testing fit, assumptions, and risk.

        CRITICAL BASELINE:
        - Base judgments ONLY on the February 2026 resume text provided below.
        - Assume the candidate will fail unless proven otherwise.
        - Default to disqualification, not qualification.

        ADDITIONAL CRITERIA (SPECIFIC TO MIKE THORN):
        - Ignore requirements for 'Bachelor's degree' if the job description includes "or equivalent experience" or similar. Higher-level degrees should be considered a hard blocker.
        - Jobs which require a commute are a hard 'no.' Make exceptions for "occasional travel" or "visits" to offices or offsites, but not weekly commutes. 
        - Jobs requring 'Public Trust' or 'Secret'/'Top Secret' clearance are a no-go.
        - If a job requires hardcore "startup" or "bootstrapped" or "scaleup" experience, it should be a 'no'. Note this clearly in the kill_criteria section.
        - Jobs with a hard requirement (or very strong focus on) DICOM, HL7/FHIR, or HL7v2 should be a 'no'. Note this clearly in the kill_criteria section.
        - I am 'proficient' in Powershell, with "working familiarity" of SQL, and Typescript. I can read Python, Ruby, and React. If a job hard-requires proficiency in other languages, or skills beyond these I've just stated, the job should be a 'no'. Note this clearly in the kill_criteria section.
        - I have a working familiarity with Linux, and slightly less familiarity with MacOS (although I have used it). If a job requires admin-level proficiency in Linux or MacOS, the job should be a 'no'. Note this clearly in the kill_criteria section.
        - I have no experience with kubernetes, docker, or other containerization technologies. If a job has a hard requirement of proficiency in these technologies, the job should be a 'no'. Note this clearly in the kill_criteria section.
        - I have no experience with Epic, Figma, or Salesforce. If a job has a hard requirement of proficiency in these technologies, the job should be a 'no'. Note this clearly in the kill_criteria section.

        GENERAL RULES
        - Look for indicators of the timezone that is desired for the job. Note this finding in the job facts section.
        - Jobs which require many domain-specific skills or software experience that aren't on the baseline resume, or aren't closely adjacent, should weight heavier toward 'no' than a job where only one or two requirements or "nice to haves" are adjacent or deeper on a skill that the baseline resume shows.
        - In general, if the candidate qualifies for all but one or two of the job requirements, the job should be a 'maybe', or 'yes' if these requirements can be reasonably overcome by the candidate (i.e. experience in a specific application is not a blocker, but experience in a specific industry is a blocker, etc.).
        - If the job is a no-go or a no, briefly explain why in the kill_criteria section.
        - If the job is a maybe or a yes, briefly explain why in the if_apply_anyway section.

        OUTPUT FORMAT (STRICT):
        Return VALID JSON with these keys exactly:
        - verdict: one of ["NO","MAYBE","YES"]
        - job_facts_extracted: object
        - real_job_decoded: object
        - hostile_requirements: object
        - fit_exposure_vs_resume: object
        - survivability_vs_excellence: object
        - leverage_and_asymmetry: object
        - human_cost_politics: object
        - kill_criteria: array of strings (3-7 items)
        - if_apply_anyway: object

        Keep each object concise but specific (bullets as arrays of strings where useful).
        Do NOT use semicolons within any string values—use periods, commas, or em-dashes instead.
        Semicolons are reserved as delimiters for display formatting.

        === Candidate resume (Feb 2026) ===
        {resume_text}

        === Job facts (from scraping) ===
        {facts}

        === Job description (raw) ===
        {job.description}
        """
    ).strip()


def evaluate_jobs_anthropic(
    *,
    ai_cfg: AIConfig,
    jobs: list[JobPost],
    out_dir: str | Path,
) -> list[dict[str, Any]]:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("Missing ANTHROPIC_API_KEY environment variable.")

    out_dir = Path(out_dir)
    cache_dir = ensure_dir(out_dir / "cache" / "ai")
    log.info("Anthropic eval: %s jobs (model=%s)", len(jobs), ai_cfg.model)

    resume_path = Path(ai_cfg.resume_path)
    if not resume_path.exists():
        # allow relative to project root by searching current working dir
        raise FileNotFoundError(f"Resume baseline not found at: {resume_path}")
    resume_text = resume_path.read_text(encoding="utf-8")
    resume_hash = sha256_text(resume_text)

    client = Anthropic(api_key=api_key)

    results: list[dict[str, Any]] = []
    for job in jobs:
        log.debug("Evaluating job_id=%s", job.job_id)
        prompt = _build_prompt(resume_text=resume_text, job=job)
        job_hash = sha256_text(prompt + f"|model={ai_cfg.model}|resume={resume_hash}")
        cache_path = cache_dir / f"{job.job_id}.json"
        if cache_path.exists():
            try:
                cached = __import__("json").loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("input_hash") == job_hash:
                    log.debug("Cache hit for job_id=%s", job.job_id)
                    # Mark cache hits so downstream code can notify only on new evals.
                    if isinstance(cached, dict) and "_cache_hit" not in cached:
                        cached = {**cached, "_cache_hit": True}
                    results.append(cached)
                    continue
            except Exception:
                pass

        msg = client.messages.create(
            model=ai_cfg.model,
            max_tokens=int(ai_cfg.max_tokens),
            temperature=float(ai_cfg.temperature),
            messages=[
                {"role": "user", "content": prompt},
            ],
        )

        text = ""
        # anthropic SDK returns list of content blocks; collect text blocks
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                text += block.text

        payload: dict[str, Any]
        parsed = _parse_json_best_effort(text)
        if parsed is not None:
            payload = dict(parsed)
            if getattr(msg, "stop_reason", None) == "max_tokens":
                payload["_meta"] = {"truncated": True, "method": "json_parse"}
        else:
            res = parse_raw_text_to_parsed(text)
            payload = {
                "raw_text": text,
                "parsed": {
                    "_meta": {
                        "method": res.method,
                        "truncated": bool(res.truncated),
                    },
                    **res.parsed,
                },
            }

        record = {
            "job_id": job.job_id,
            "job_url": job.job_url,
            "input_hash": job_hash,
            "model": ai_cfg.model,
            "output": payload,
            "_cache_hit": False,
        }
        dump_json(cache_path, record)
        results.append(record)

    log.info("Anthropic eval done (%s records)", len(results))
    return results

