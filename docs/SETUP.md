## Install + setup (bulletproof)

This project is currently optimized for **Windows 10+ + PowerShell**, but it runs anywhere Playwright + Python run.

### Prerequisites

- **Python**: 3.10+ (`python --version`)
- **A LinkedIn account**: you will authenticate once in a real browser window
- **Anthropic API key** (only if you want AI eval): set `ANTHROPIC_API_KEY`
- Optional: **Resend API key** (only if you enable notifications): set `RESEND_API_KEY` or `notifications.resend_api_key`

### 0) Open a terminal in the repo root

```powershell
cd C:\path\to\linkedin-siftr
```

### 1) Create and activate a virtualenv

From the repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

If PowerShell blocks script execution:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

Confirm you’re using the venv Python:

```powershell
python -c "import sys; print(sys.executable)"
```

### 2) Install Python dependencies

```powershell
pip install -r requirements.txt
```

### 3) Install Playwright’s Chromium

```powershell
python -m playwright install chromium
```

If this fails, try upgrading pip first:

```powershell
python -m pip install --upgrade pip
python -m playwright install chromium
```

### 4) Create your local config

```powershell
copy config.example.yaml config.yaml
```

Edit `config.yaml` to your taste. Full reference is in `docs/CONFIGURATION.md`.

### 5) Provide your resume baseline file

The evaluator reads `ai.resume_path` (default is `resume-feb-2026.md`).

- Put your resume baseline at that path, or
- Change `ai.resume_path` to point at your preferred file.

Important: **Do not commit your resume** if you plan to open-source this repo.

### 6) Set required environment variables

#### Anthropic (required for AI eval)

For *current PowerShell session*:

```powershell
$env:ANTHROPIC_API_KEY = "your-key-here"
```

To persist for future terminals (requires opening a new terminal afterward):

```powershell
setx ANTHROPIC_API_KEY "your-key-here"
```

Notes:
- If you use `setx`, **restart PowerShell (and Cursor)** so the new environment is inherited.
- If you want “scrape/export only”, set `run.max_ai_evals_per_run: 0` and you do **not** need `ANTHROPIC_API_KEY`.

#### Resend (only if notifications are enabled)

```powershell
$env:RESEND_API_KEY = "re_..."
```

### 7) First run: interactive login

This opens a real browser window and waits for you to complete LinkedIn auth (MFA/passkeys/checkpoints):

```powershell
python -m siftr.run --config config.yaml --login
```

After this succeeds, your authenticated session is stored in `scrape.browser.user_data_dir` (default: `out/profiles/chromium`) and subsequent runs can be headless.

### 8) Normal runs

```powershell
python -m siftr.run --config config.yaml
```

### macOS / Linux notes

- Use `python3` instead of `python` if needed.
- Virtualenv activation is usually:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

### Uninstall / reset

- **Force a fresh LinkedIn login**: delete the profile directory at `scrape.browser.user_data_dir` (default `out/profiles/chromium/`), then rerun with `--login`.
- **Clear AI cache**: delete `out/cache/ai/` (you’ll re-evaluate jobs next run).

