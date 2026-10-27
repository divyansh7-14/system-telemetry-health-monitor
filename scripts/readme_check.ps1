# Runs the README's Windows instructions, as written, on a fresh machine.
# Lines ending in "# README" must appear verbatim in README.md (tests/test_readme.py).
# Used by the "Windows demo and README check" workflow (Windows PowerShell 5.1).
$ErrorActionPreference = 'Stop'
function Check($what) { if ($LASTEXITCODE -ne 0) { throw "$what failed (exit code $LASTEXITCODE)" } }
function Lines($path) { if (Test-Path $path) { @(Get-Content $path | Where-Object { $_.Trim() }).Count } else { 0 } }
function WaitLines($path, $count, $seconds) {
  $end = (Get-Date).AddSeconds($seconds)
  while ((Lines $path) -lt $count) {
    if ((Get-Date) -gt $end) { throw "timed out waiting for $count line(s) in $path" }
    Start-Sleep -Milliseconds 500
  }
}
function LastLine($path) { (Get-Content $path | Select-Object -Last 1) }

Set-Location $env:RUNNER_TEMP
Write-Host "== Install"
git clone https://github.com/divyansh7-14/system-telemetry-health-monitor.git  # README
Check "git clone"
cd system-telemetry-health-monitor  # README
python -m venv .venv  # README
Check "python -m venv"
.venv\Scripts\python -m pip install .  # README
Check "pip install"

Write-Host "== Quick start"
.venv\Scripts\sysmon --once  # README
Check "sysmon --once"
if ((Lines logs\metrics.jsonl) -ne 1) { throw "--once should write one metrics line" }
.venv\Scripts\sysmon --help  # README
Check "sysmon --help"

Write-Host "== Background with pythonw, stopped with --stop"
$before = Lines logs\metrics.jsonl
Start-Process .venv\Scripts\pythonw.exe -ArgumentList "-m sysmon --output-dir `"$PWD\logs`" --threshold-config `"$PWD\config\config.yaml`""  # README
WaitLines logs\metrics.jsonl ($before + 2) 60
.venv\Scripts\sysmon --stop --output-dir logs  # README
Check "sysmon --stop"
if ((LastLine logs\agent.log) -notmatch '\(sysmon --stop\)') { throw "agent.log should end with the --stop line" }
if (Test-Path logs\sysmon.lock) { throw "lock file left behind" }

Write-Host "== Task Scheduler"
$before = Lines logs\metrics.jsonl
$action  = New-ScheduledTaskAction -Execute "$PWD\.venv\Scripts\pythonw.exe" -Argument "-m sysmon --output-dir `"$PWD\logs`" --threshold-config `"$PWD\config\config.yaml`""  # README
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME  # README
Register-ScheduledTask -TaskName "sysmon-agent" -Action $action -Trigger $trigger  # README
Start-ScheduledTask -TaskName "sysmon-agent"  # README
try {
  WaitLines logs\metrics.jsonl ($before + 2) 60
  .venv\Scripts\sysmon --stop --output-dir logs  # README
  Check "sysmon --stop (scheduled task)"
  if ((LastLine logs\agent.log) -notmatch '\(sysmon --stop\)') { throw "scheduled agent did not stop cleanly" }
} finally {
  Unregister-ScheduledTask -TaskName "sysmon-agent" -Confirm:$false  # README
}
Write-Host "README instructions work as written."
# Evidence, readable from the run summary: what the agent itself logged.
$log = (Get-Content logs\agent.log | ForEach-Object { $_ -replace '%', '%25' }) -join '%0A'
Write-Output "::notice title=README check passed - agent.log::$log"
