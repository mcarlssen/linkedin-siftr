## Scheduling (Windows Task Scheduler)

The reliable way to run this hourly is to **call the repo’s venv Python directly** (or a wrapper that does so), and to ensure the **working directory is the repo root** so relative paths like `config.yaml`, `out/`, and `resume-feb-2026.md` resolve correctly.

This repo includes a wrapper script:

- `scripts/run_scheduled.ps1`

It:
- `cd`s to the repo root
- runs `.\.venv\Scripts\python.exe -m siftr.run --config config.yaml`
- appends logs to `out/logs/scheduled.YYYYMMDD.log`

### 1) Make environment variables persistent (recommended)

If you run AI evals, Task Scheduler must have `ANTHROPIC_API_KEY` available.

From PowerShell (run once), then **restart PowerShell/Cursor**:

```powershell
setx ANTHROPIC_API_KEY "your-key-here"
setx RESEND_API_KEY "re_your_key_here"  # optional (only if notifications enabled)
```

Note: If you run the task under a different Windows account than you tested with, you must set env vars for that account too.

### 2) Create the scheduled task

Open **Task Scheduler** → **Create Task…** (not “Basic Task”).

#### General tab

- **Name**: `siftr hourly`
- **Run only when user is logged on**: required so the task can use your Chromium profile (cookies/session) under `out/profiles/chromium`
- **Configure for**: Windows 10

#### Triggers tab

Create one trigger:

- **Begin the task**: On a schedule → Daily
- **Start**: `6:00:00 AM`
- Check **Repeat task every**: `1 hour`
- **for a duration of**: `18 hours`

This runs at 6, 7, 8, …, 11pm and stops before midnight.

#### Actions tab

Action: **Start a program**

- **Program/script**: `powershell.exe`
- **Add arguments**:

```text
-NoProfile -ExecutionPolicy Bypass -File "C:\Users\mthorn\linkedin-siftr\scripts\run_scheduled.ps1"
```

- **Start in**:

```text
C:\Users\mthorn\linkedin-siftr
```

Optional: add `-Verbose` to your wrapper for more logging:

```text
-NoProfile -ExecutionPolicy Bypass -File "C:\Users\mthorn\linkedin-siftr\scripts\run_scheduled.ps1" -Verbose
```

#### Settings tab (important)

To prevent overlapping runs:

- **If the task is already running**: **Do not start a new instance**
- Optional: **Stop the task if it runs longer than**: `55 minutes`

### Troubleshooting

- If it works in your terminal but not in Task Scheduler, it’s usually one of:
  - wrong “Start in” directory
  - task running as a different Windows user (no env vars / no access to `out/profiles/`)
  - missing `ANTHROPIC_API_KEY` (only required if `run.max_ai_evals_per_run > 0`)
