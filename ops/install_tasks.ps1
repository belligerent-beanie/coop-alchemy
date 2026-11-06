<#
  Registers the Windows scheduled tasks that keep the live server running without you. NOT run automatically: read it,
  then run it yourself (no administrator rights needed; the tasks run as you, while you are logged on).

      powershell -ExecutionPolicy Bypass -File ops\install_tasks.ps1 [-LiveDir C:\...\coop-alchemy-live] [-Python C:\...\python.exe]
      powershell -ExecutionPolicy Bypass -File ops\install_tasks.ps1 -Remove        removes them again

  Tasks:
    CoopAlchemy-Start     at logon: start the server if it is not already running
    CoopAlchemy-Watchdog  every minute: restart the server if it has stopped answering (ops\watchdog.py)
    CoopAlchemy-Backup    every day at 04:00: back up the live data (ops\backup.py), keeping the newest 14
    CoopAlchemy-Summary   every day at 04:30: write yesterday's counts to <live>\summaries (ops\daily_summary.py)

  Limits: the tasks run only while you are logged on, so after a reboot the server comes back once you log in (turn on
  automatic sign-in for this Windows account if the PC must recover on its own). The PC also has to stay awake: set
  Settings > System > Power > Screen and sleep > "When plugged in, put my device to sleep after" to Never.
#>
param(
    [string]$LiveDir = (Join-Path (Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)) "..\coop-alchemy-live"),
    [string]$Python = "",
    [switch]$Remove
)

$names = "CoopAlchemy-Start", "CoopAlchemy-Watchdog", "CoopAlchemy-Backup", "CoopAlchemy-Summary"
if ($Remove) {
    foreach ($name in $names) { Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue }
    Write-Host "Removed the Co-op Alchemy scheduled tasks."
    exit 0
}

$live = (Resolve-Path $LiveDir).Path
if (-not $Python) {
    if (Test-Path "$live\python-path.txt") { $Python = (Get-Content "$live\python-path.txt" -TotalCount 1).Trim() }
    else { $Python = (Get-Command python).Source }
}
if (-not (Test-Path "$live\ops\restart-live.ps1")) { Write-Error "$live\ops\restart-live.ps1 is missing: deploy a version that includes it first."; exit 1 }
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$start = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $live `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$live\ops\restart-live.ps1`" -OnlyIfDown"
Register-ScheduledTask -TaskName "CoopAlchemy-Start" -Action $start -Settings $settings -Force `
    -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $me) | Out-Null

$watch = New-ScheduledTaskAction -Execute $Python -WorkingDirectory $live -Argument "ops\watchdog.py --live-dir `"$live`""
$every = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName "CoopAlchemy-Watchdog" -Action $watch -Settings $settings -Trigger $every -Force | Out-Null

$backup = New-ScheduledTaskAction -Execute $Python -WorkingDirectory $live -Argument "ops\backup.py --live-dir `"$live`""
Register-ScheduledTask -TaskName "CoopAlchemy-Backup" -Action $backup -Settings $settings -Force `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 4:00AM) | Out-Null

$summary = New-ScheduledTaskAction -Execute $Python -WorkingDirectory $live -Argument "ops\daily_summary.py --live-dir `"$live`""
Register-ScheduledTask -TaskName "CoopAlchemy-Summary" -Action $summary -Settings $settings -Force `
    -Trigger (New-ScheduledTaskTrigger -Daily -At 4:30AM) | Out-Null

Write-Host "Registered: $($names -join ', ')  (live folder: $live, Python: $Python)"
