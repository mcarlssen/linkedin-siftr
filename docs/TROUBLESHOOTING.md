## Troubleshooting

### “Not logged in… redirected to a login/checkpoint page”

Cause: LinkedIn redirected Playwright to `/login` or `/checkpoint`.

Fix:
- Re-run with `--login` and complete auth in the opened browser window.

```powershell
python -m siftr.run --config config.yaml --login
```

If you want to force a completely fresh login:
- delete `scrape.browser.user_data_dir` (default `out/profiles/chromium/`)
- rerun with `--login`

### “Could not find any job cards before timeout”

Common causes:
- You’re still on a LinkedIn checkpoint page
- LinkedIn changed the DOM
- Your account is rate-limited / served a degraded page

What to check:
- Look in `out/debug/` for:
  - `top_applicant_load_failed.png`
  - `top_applicant_load_failed.html`

Mitigations:
- Run with `--login` so you can see what LinkedIn is showing.
- Increase pacing:
  - increase `scrape.rate_limit.min_delay_seconds` / `max_delay_seconds`
  - increase `scrape.browser.slow_mo_ms`

### “Missing ANTHROPIC_API_KEY environment variable”

Fix for current PowerShell session:

```powershell
$env:ANTHROPIC_API_KEY = "your-key-here"
```

Or run scrape-only:
- set `run.max_ai_evals_per_run: 0`

### Resume baseline not found

Error looks like:
- `FileNotFoundError: Resume baseline not found at: ...`

Fix:
- Ensure the file exists at `ai.resume_path` (relative paths are resolved from your current working directory), or update `ai.resume_path` in `config.yaml`.

### Notification email fails (Resend)

If `notifications.enabled: true`, the run will error if:
- no API key was found (`notifications.resend_api_key` or `RESEND_API_KEY`)
- `notifications.from_email` is missing or not verified in Resend
- `notifications.to_emails` is empty

Tip:
- Notifications only send when the run produces **new** AI evaluations (not cache hits). If you’re testing email, clear `out/cache/ai/` or change the model/resume so the run generates new evals.

### Runs are “stuck” / slow

Causes:
- Conservative rate limits by design
- LinkedIn loading delays / throttling

Actions:
- Use `--verbose` to see progress.
- Lower `scrape.max_jobs` while you iterate.

### Cache behavior surprises

AI cache is keyed by:
- the job’s `job_id`
- the full prompt (includes scraped facts + job description)
- `ai.model`
- the hash of your resume baseline file

So changing your resume or model will trigger fresh evals (expected).

