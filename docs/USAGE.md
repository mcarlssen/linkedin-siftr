## Usage

### Basic run (headless by default)

```powershell
python -m siftr.run --config config.yaml
```

### First run / re-auth (visible browser)

```powershell
python -m siftr.run --config config.yaml --login
```

`--login` forces a visible browser and pauses if LinkedIn redirects to a login/checkpoint page.

### Verbose logs

```powershell
python -m siftr.run --config config.yaml --verbose
```

### Scrape-only (no AI key required)

Set this in `config.yaml`:

```yaml
run:
  max_ai_evals_per_run: 0
```

Then run normally:

```powershell
python -m siftr.run --config config.yaml
```

### Multiple collections

You can configure multiple named collection URLs under `run.collections` and choose them at runtime.

Non-interactive selection:

```powershell
python -m siftr.run --config config.yaml --collections top_applicant,recommended
```

Interactive multi-select:

```powershell
python -m siftr.run --config config.yaml --choose-collections
```

Default behavior:
- If you don’t specify `--collections` (or use `--choose-collections`), runs default to **all** configured collections.

### Single-job mode (for evaluator tuning)

Scrape and analyze a single job posting URL:

```powershell
python -m siftr.run --config config.yaml --job-url "https://www.linkedin.com/jobs/view/4369155696"
```

If you need to authenticate first:

```powershell
python -m siftr.run --config config.yaml --login --job-url "https://www.linkedin.com/jobs/view/4369155696"
```

Single-job mode nuance:
- The job will be AI-evaluated (if `run.max_ai_evals_per_run > 0`) **even if it fails prefilters**, so you can iterate on prompts without fighting filter logic.

### Output files

Outputs are controlled by `export.xlsx_path`, `export.csv_path`, and `export.datestamp`.

- **XLSX**: 3 sheets:
  - `run_summary`
  - `all_jobs`
  - `passed_ai`
- **CSV**: 3 files derived from `export.csv_path`:
  - `<stem>.run_summary.csv`
  - `<stem>.all_jobs.csv`
  - `<stem>.passed_ai.csv` (omits `description` for quick triage)

If `export.datestamp: true`, a stamp is inserted before the extension (or use `{stamp}` in the path for full control).

### AI caching

AI outputs are cached at:

- `out/cache/ai/<job_id>.json`

Cache validity includes:
- the full prompt (which includes scraped job facts + job description)
- `ai.model`
- the hash of your resume baseline file (`ai.resume_path`)

Changing any of those will invalidate the cache and trigger a fresh AI eval.

### Seen-job caching (scrape efficiency)

To avoid re-scanning the same LinkedIn collection results on every run, enable `scrape.seen_cache` in `config.yaml`.

When enabled, the scraper will skip opening/clicking jobs that were fully scanned within the configured window (`skip_if_scanned_within_days`).

### Run logs (for scheduled/overnight runs)

Each run writes a structured JSON log under:

- `out/logs/run.<stamp>.json`

The log includes the full scrape list, per-job prefilter results, which jobs were sent to AI (and cache-hit status), and which jobs were emailed.

