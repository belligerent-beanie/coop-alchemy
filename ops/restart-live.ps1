<#
  Restarts the LIVE Co-op Alchemy server from this checkout. Players' data lives in <live folder>\data, separate from
  the development folder. Normally you do not run this yourself: ops\deploy.py and ops\watchdog.py do.

  .\ops\restart-live.ps1               stop whatever listens on the port, then start the server
  .\ops\restart-live.ps1 -OnlyIfDown   start the server only if nothing is listening (used at logon)
  .\ops\restart-live.ps1 -Update       first fast-forward to main WITHOUT any checks (prefer: python ops\deploy.py)

  It refuses to run anywhere except a checkout on a branch called live*, so it can never start the development folder
  with development data by mistake. Python is found from $env:ALCHEMY_PYTHON, then the first line of python-path.txt in the
  live folder, then `python` on the PATH. Server messages go to data\live.err.log (and data\live.log once logging to a
  file is configured). A restart ends running games (saved games stay) and everyone has to log in again.
#>
param([switch]$Update, [switch]$OnlyIfDown, [int]$Port = 8000)

$ops = Split-Path -Parent $MyInvocation.MyCommand.Path
$here = Split-Path -Parent $ops
Set-Location $here

$branch = (git rev-parse --abbrev-ref HEAD 2>$null)
if ($branch -notlike "live*") {
    Write-Error "This folder is on branch '$branch', not a live branch: refusing to touch the live server from here."
    exit 1
}

$python = $env:ALCHEMY_PYTHON
if (-not $python -and (Test-Path "$here\python-path.txt")) { $python = (Get-Content "$here\python-path.txt" -TotalCount 1).Trim() }
if (-not $python) { $python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $python -or -not (Test-Path $python)) {
    Write-Error "Cannot find Python: set ALCHEMY_PYTHON or put its full path in $here\python-path.txt"
    exit 1
}

if ($Update) {
    git merge --ff-only main
    if ($LASTEXITCODE -ne 0) { Write-Error "Could not fast-forward to main; nothing was restarted."; exit 1 }
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener -and $OnlyIfDown) {
    Write-Host "Server already running on port $Port (process $($listener.OwningProcess))"
    exit 0
}
if ($listener) {
    Write-Host "Stopping the server on port $Port (process $($listener.OwningProcess))"
    Stop-Process -Id $listener.OwningProcess -Force
    Start-Sleep -Seconds 1
}

New-Item -ItemType Directory -Force data | Out-Null
$serverArgs = @(
    "server.py", "--port", "$Port",
    "--accounts-file", "data\accounts.json", "--daily-file", "data\daily.json", "--drafts-file", "data\drafts.json",
    "--invites-file", "data\invites.json", "--reports-file", "data\reports.json",
    "--saves-dir", "data\saves", "--snapshot-file", "data\reload_snapshot.json", "--pid-file", "data\alchemy.pid",
    "--log-file", "data\live.log", "--log-days", "14"
)
Start-Process -FilePath $python -ArgumentList $serverArgs -WorkingDirectory $here -WindowStyle Hidden `
    -RedirectStandardOutput "data\live.out.log" -RedirectStandardError "data\live.err.log"
Start-Sleep -Seconds 3
if (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue) {
    Write-Host "Live server is up on port $Port, running $(git log --oneline -1)"
} else {
    Write-Error "The server did not start; see data\live.err.log"
    exit 1
}
