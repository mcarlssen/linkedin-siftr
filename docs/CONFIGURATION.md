## Configuration reference (`config.yaml`)

The app is configured entirely via YAML. Start from `config.example.yaml` and copy to `config.yaml`.

```powershell
copy config.example.yaml config.yaml
```

### Root keys

- **`run`**: run-scoped settings (collections, outputs, AI cap)
- **`scrape`**: scraping limits and browser behavior
- **`filters`**: deterministic prefilters and rule engine
- **`ai`**: AI provider/model and resume baseline path
- **`export`**: output files and datestamping
- **`notifications`**: optional email notifications

---

## `run`

### `run.out_dir` (required)

Output directory, relative to project root (unless you provide an absolute path).

```yaml
run:
  out_dir: "out"
```

### Collection URLs

You can configure collection URLs in either of these ways:

#### Preferred: `run.collections` (named URLs)

Names are used by CLI flags like `--collections top_applicant,recommended`.

```yaml
run:
  collections:
    top_applicant: "https://www.linkedin.com/jobs/collections/top-applicant/"
    recommended: "https://www.linkedin.com/jobs/collections/recommended/"
```

#### Back-compat: `run.<name>_url`

Any key ending with `_url` is also treated as a collection name.

```yaml
run:
  top_applicant_url: "https://www.linkedin.com/jobs/collections/top-applicant/"
  recommended_url: "https://www.linkedin.com/jobs/collections/recommended/"
```

### `run.max_ai_evals_per_run`

Maximum number of jobs to send to AI **per run** (after prefilters).

```yaml
run:
  max_ai_evals_per_run: 30
```

Notes:
- Set to **0** for “scrape/export only” mode (no AI, no `ANTHROPIC_API_KEY` requirement).
- In multi-collection runs, jobs are de-duped by `job_id` before filtering.

---

## `scrape`

### `scrape.max_jobs`

Soft cap for how many job cards to load/extract per collection. Prevents endless scrolling.

```yaml
scrape:
  max_jobs: 200
```

### `scrape.seen_cache`

Persistent cache of seen job IDs across runs. When enabled, the scraper will **skip opening/clicking** jobs that were fully scanned recently.

```yaml
scrape:
  seen_cache:
    enabled: true
    path: "out/cache/seen_jobs.json"
    skip_if_scanned_within_days: 7
```

Notes:
- If `path` is left at the default, the cache is written under `run.out_dir` as: `<out_dir>/cache/seen_jobs.json`.
- Skipped jobs are still marked as “seen” in the cache (timestamp updates), but their details are not re-scraped.

### `scrape.browser`

```yaml
scrape:
  browser:
    user_data_dir: "out/profiles/chromium"
    headless: true
    slow_mo_ms: 75
```

- **`user_data_dir`**: persistent Chromium profile location (cookies/session). Delete it to force re-login.
- **`headless`**: default headless setting. If you run with `--login`, the browser will be visible regardless of this value.
- **`slow_mo_ms`**: adds a small delay to Playwright actions to reduce flakiness / rate limiting.

### `scrape.rate_limit`

Randomized sleep between interactions (jitter) to reduce blocks.

```yaml
scrape:
  rate_limit:
    min_delay_seconds: 1.0
    max_delay_seconds: 3.0
```

---

## `filters`

### `filters.remote_only`

```yaml
filters:
  remote_only: true
```

Remote detection uses:
- LinkedIn top-card metadata (`remote` / `hybrid` / `on-site`)
- fallback keyword heuristics in the job description (e.g. rejects if “hybrid/on-site/onsite” appears)

### `filters.usa_only`

```yaml
filters:
  usa_only: true
```

USA detection uses:
- text markers like “United States”, “USA”, “U.S.”
- a conservative state-abbreviation heuristic (e.g. matches “Chicago, IL” / “(IL)”)

### `filters.max_age_days`

Optional: skip postings older than N days (approximate; uses LinkedIn "X days/weeks/months ago" text).

```yaml
filters:
  max_age_days: 7
```

### `filters.allow_missing_date_posted`

When `max_age_days` is set and the posting age cannot be scraped: if `true`, the job passes; if `false`, it is rejected.

```yaml
filters:
  max_age_days: 7
  allow_missing_date_posted: true
```

### `filters.skip_already_applied`

Skip roles that LinkedIn marks as already applied (detected via “See application” / “Applied …” signals).

```yaml
filters:
  skip_already_applied: true
```

If `true`, prefilter will reject with reason `reject.already_applied`.

### `filters.compensation`

```yaml
filters:
  compensation:
    allow_missing: true
    min_annual_usd: 75000
    min_hourly_usd: 38
```

Behavior:
- If pay range is missing or unparseable, the job passes compensation filtering **only if** `allow_missing: true`.
- Pay range parsing is best-effort from LinkedIn “Pay range” text and currently assumes USD heuristics.

### `filters.company_blocklist`

```yaml
filters:
  company_blocklist:
    exact:
      - "Robert Half"
      - "TEKsystems"
    regex:
      - "(?i)staffing"
      - "(?i)recruit(ing|er)"
```

Notes:
- **`exact`** is a direct string match against the scraped company name.
- **`regex`** patterns are evaluated against the scraped company name.
- Malformed regex patterns are ignored (they won’t crash the run).

### `filters.skip_rules` (rule engine)

Skip rules are evaluated after the other filters. A rule matches when **all** conditions match; a match triggers the rule’s `action` (currently only `skip`).

```yaml
filters:
  skip_rules:
    - id: "skip.solutions_engineer.high_floor"
      when:
        - field: title
          op: regex
          value: "(?i)\\bsolutions\\s+engineer\\b"
        - field: comp_min_annual_usd
          op: gte
          value: 120000
      action: "skip"
      reason: "Solutions Engineer roles with high comp floor tend to be a bad fit."
```

#### Supported `field` values

- `title`
- `company`
- `location_text`
- `description`
- `is_remote` (tri-state: `true` / `false` / `null` when unknown)
- `comp_min_annual_usd`
- `comp_max_annual_usd`
- `comp_min_hourly_usd`
- `comp_max_hourly_usd`

#### Supported `op` values

- String ops: `contains`, `regex`, `equals`, `in`
- Numeric ops: `gte`, `gt`, `lte`, `lt`
- Tri-state ops (for `is_remote`): `is_true`, `is_false`, `is_unknown`

#### Notes / gotchas

- `in` expects `value` to be a list (YAML sequence).
- `regex` only applies to string fields. For company blocklist regex, malformed patterns are ignored; for `skip_rules` regex, **invalid patterns will raise a regex error and fail the run**, so keep them valid.
- Compensation fields are only populated when LinkedIn provides a parseable pay range.

---

## `ai`

```yaml
ai:
  provider: "anthropic"
  model: "claude-3-5-sonnet-20241022"
  max_tokens: 1400
  temperature: 0.2
  resume_path: "resume-feb-2026.md"
```

- **`provider`**: currently only `anthropic`
- **`model`**: any Anthropic model name your account has access to
- **`max_tokens` / `temperature`**: forwarded to the Anthropic Messages API
- **`resume_path`**: path to the resume baseline text file (required for AI eval)

Environment:
- **`ANTHROPIC_API_KEY`** must be set if `run.max_ai_evals_per_run > 0`.

---

## `export`

At least one of `xlsx_path` or `csv_path` is required.

```yaml
export:
  xlsx_path: "out/jobs.xlsx"
  # csv_path: "out/jobs.csv"
  datestamp: true
  datestamp_format: "%Y%m%d-%H%M%S"
```

- **`xlsx_path`**: writes one workbook with 3 sheets.
- **`csv_path`**: writes 3 CSV files derived from this path’s stem.
- **`datestamp`**: if true, inserts `.<stamp>` before the extension.
- **`datestamp_format`**: UTC timestamp format used for the stamp.

Stamp placeholder:
- If your path contains `{stamp}`, it will be replaced directly (this overrides `datestamp` insertion behavior).

Example:

```yaml
export:
  csv_path: "out/jobs.{stamp}.csv"
```

---

## `notifications` (optional)

Notifications are sent **only when the run produced new AI evaluations** (cache hits do not trigger email).

```yaml
notifications:
  enabled: false
  provider: "resend"
  resend_api_key: null
  from_email: "onboarding@resend.dev"
  to_emails:
    - "you@example.com"
  subject: "LinkedIn job eval — {new_ai_count} new AI result(s)"
```

- **`enabled`**: master toggle
- **`provider`**: currently only `resend`
- **`resend_api_key`**: optional; if omitted, `RESEND_API_KEY` environment variable is used
- **`from_email`**: must be a verified sender in Resend
- **`to_emails`**: list of recipients (a single string is also accepted)
- **`subject`**: Python `.format(...)` template; supports:
  - `{new_ai_count}`
  - plus common run metadata keys (e.g. `{scraped_count}`, `{passed_prefilter_count}`, `{xlsx_path}`, `{csv_path}`)

Recipient key aliases:
- `notifications.to_emails` is preferred
- `notifications.to` and `notifications.to_email` are also accepted and will be normalized into `to_emails`

