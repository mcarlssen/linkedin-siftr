# Run siftr for Task Scheduler. Use this script (not python.exe) so cwd is set correctly.
# Config paths (out_dir, user_data_dir) are resolved from the config file's directory,
# so the same Chromium profile is used regardless of Task Scheduler "Start in" setting.
param(
  [string]$ConfigPath = "config.yaml",
  [switch]$Verbose
)

$ErrorActionPreference = "Stop"

# Repo root = parent of /scripts; ensure Python runs with repo as cwd.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\\Scripts\\python.exe"
if (-not (Test-Path $Python)) {
  throw "Expected venv Python not found at: $Python`nCreate it from repo root: python -m venv .venv"
}

# Make sure output/log dirs exist.
$OutDir = Join-Path $RepoRoot "out"
$LogsDir = Join-Path $OutDir "logs"
New-Item -ItemType Directory -Force $LogsDir | Out-Null

# Per-run log file (no sharing, no lock conflicts when runs overlap).
$StampTime = Get-Date -Format "yyyyMMdd-HHmmss"
$LogPath = Join-Path $LogsDir ("scheduled.{0}.log" -f $StampTime)

$env:PYTHONUTF8 = "1"
# Unbuffer stdout/stderr so we see logs immediately; otherwise output is buffered until Python exits.
$env:PYTHONUNBUFFERED = "1"

$PyArgs = @("-m", "siftr.run", "--config", $ConfigPath)
if ($Verbose) { $PyArgs += "--verbose" }

# All log writes UTF-8 so the file is plain text (readable everywhere).
"[{0}] Starting siftr run: {1} {2}" -f (Get-Date -Format s), $Python, ($PyArgs -join " ") | Out-File -FilePath $LogPath -Encoding utf8

# Redirect Python stdout/stderr to files so we always capture output when run from Task Scheduler
# (piping through PowerShell can lose output when the task spawns a visible console).
$StdoutFile = Join-Path $LogsDir ("scheduled.{0}.stdout.txt" -f $StampTime)
$StderrFile = Join-Path $LogsDir ("scheduled.{0}.stderr.txt" -f $StampTime)
$p = Start-Process -FilePath $Python -ArgumentList $PyArgs -WorkingDirectory $RepoRoot -NoNewWindow -Wait -PassThru -RedirectStandardOutput $StdoutFile -RedirectStandardError $StderrFile
$ExitCode = $p.ExitCode
# Merge stdout then stderr into the main log (UTF-8).
Get-Content -Path $StdoutFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue | Out-File -FilePath $LogPath -Append -Encoding utf8
Get-Content -Path $StderrFile -Raw -Encoding UTF8 -ErrorAction SilentlyContinue | Out-File -FilePath $LogPath -Append -Encoding utf8
Remove-Item -Path $StdoutFile, $StderrFile -Force -ErrorAction SilentlyContinue

"[{0}] Finished (exit_code={1})" -f (Get-Date -Format s), $ExitCode | Out-File -FilePath $LogPath -Append -Encoding utf8
exit $ExitCode
