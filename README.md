## siftr

**siftr helps you decide which LinkedIn jobs are worth applying to.** It scrapes your LinkedIn job collections (e.g. "Top Applicant", "Recommended"), filters out roles that don't match your criteria (remote, comp, location, company blocklist), then runs a deliberately critical AI evaluation against your resume. You get a spreadsheet of results—with a clear "pursue or skip" verdict for each job—plus optional email alerts when new matches appear.

Scrape one or more LinkedIn job *collection* pages (e.g. “Top Applicant”), apply deterministic filters, then run a deliberately critical AI evaluation against a **resume baseline**. Results export to XLSX and/or CSV, with per-job AI caching.

## What it does

- **Scrape**: Uses Playwright + a persistent Chromium profile so you can log into LinkedIn once.
- **Prefilter**: Remote/USA heuristics, comp thresholds, company blocklist, “already applied” detection, and config-driven skip rules.
- **AI eval**: Anthropic model (cached by `job_id` + prompt + model + resume hash).
- **Export**: Excel workbook (3 sheets) and/or 3 CSV files.
- **Notify (optional)**: Email via Resend *only when new AI evals happen* (not cache hits).

## Quickstart (Windows / PowerShell)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
copy config.example.yaml config.yaml

# Required if you run AI evals:
$env:ANTHROPIC_API_KEY = "your-key-here"

# Optional: Resend email notifications
$env:RESEND_API_KEY = "re_your_key_here"

# First run: open a real browser so you can login
python -m siftr.run --config config.yaml --login

# Normal run (headless by default)
python -m siftr.run --config config.yaml
```

## Scheduled task (Windows)

Use the wrapper script so the run uses the correct working directory and the **same Chromium profile** as interactive runs:

- **Program**: `powershell.exe`
- **Arguments**: `-ExecutionPolicy Bypass -File "C:\path\to\linkedin-siftr\scripts\run_scheduled.ps1"`
- **Start in**: leave empty (the script sets the repo as cwd; `out_dir` and `user_data_dir` are resolved from the config file path).

Run the task **only when the user is logged on** so it uses your Windows session and the Chromium profile under `out/profiles/chromium` (where you logged in with `--login`). Logs go to `out/logs/scheduled.*.log`.

## Email notifications (Resend)

This project can send an email **only when a run produces NEW AI evaluations** (not cache hits).

- Configure `notifications` in `config.yaml` (see `config.example.yaml`).
- Set your Resend API key via `RESEND_API_KEY` (recommended) or `notifications.resend_api_key`.
- Ensure `notifications.from_email` is a **verified sender/domain** in your Resend account.

## Documentation

- **Install + setup (bulletproof)**: [`docs/SETUP.md`](docs/SETUP.md)
- **Usage / CLI examples**: [`docs/USAGE.md`](docs/USAGE.md)
- **Configuration reference (all options)**: [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md)
- **Scheduled runs (Windows Task Scheduler)**: [`docs/SCHEDULING.md`](docs/SCHEDULING.md)
- **Troubleshooting**: [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)

## Output

By default, outputs land under `run.out_dir` (default `out/`):

- **XLSX**: `export.xlsx_path` (sheets: `run_summary`, `all_jobs`, `passed_ai`)
- **CSV**: `export.csv_path` writes:
  - `<stem>.run_summary.csv`
  - `<stem>.all_jobs.csv`
  - `<stem>.passed_ai.csv` (omits job description for quick triage)
- **AI cache**: `out/cache/ai/<job_id>.json`
- **Browser profile**: `out/profiles/chromium/` (cookies/session)

## Notes / disclaimers

- **LinkedIn ToS / automation**: scraping can violate LinkedIn’s terms and can get your account rate-limited or restricted. Use at your own risk.
- **Do not commit secrets**: keep API keys in environment variables; treat `config.yaml` as local-only (use `config.example.yaml` for sharing).

