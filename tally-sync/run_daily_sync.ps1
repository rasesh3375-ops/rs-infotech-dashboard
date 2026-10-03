# Wrapper for Windows Task Scheduler -- re-syncs the last 7 days (up to and
# including yesterday) every morning, and the whole financial year once a
# week.
#
# Why not just today: sync_tally.py with no --date syncs TODAY, and vouchers
# here get entered same-day, so a 10 AM run of today would only catch what's
# been typed into Tally by 10 AM.
#
# Why 7 days and not just yesterday: this runs on a laptop, and the first
# fortnight of running "yesterday only" left most days unsynced -- the log
# shows one scheduled run between 17 Sep and 3 Oct 2026, because the task
# never starts when the laptop is off, asleep, on battery, or logged out at
# 10 AM, and each missed morning was a day that stayed missing for good.
# Re-syncing a window means the next run that does happen fills the gap on
# its own, and it also picks up vouchers entered or corrected a few days
# late. Re-writing a day that was already synced is safe: each daily_reports
# document is rebuilt whole from Tally.
#
# Why the whole year weekly: entries made or corrected in Tally weeks after
# their date -- a late purchase bill, a month-end adjustment -- fall outside
# the 7-day window and left those days' figures as they were first synced.
# That's the likely source of a Rs.14 lakh gap found between the synced days
# and Tally for 1 Apr - 16 Sep 2026, when every single day checked against
# Tally matched exactly. It runs when the last full re-sync is a week old,
# not on a fixed weekday, because a fixed day missed (shop shut, laptop off)
# would just be skipped; and the marker is only updated when the run
# succeeds, so a failed one is retried the next morning. It takes about ten
# minutes, one Tally P&L request per day of the year plus the period reports.
#
# Requires Tally Prime open with the company loaded on THIS PC -- this only
# talks to Tally over localhost:9000, it does not launch Tally. If Tally
# isn't open, or can't read its shared data folder, the sync stops with an
# error before writing anything, and the next morning's run covers the day.

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

# Setup-Tally-Sync.cmd records which python.exe it set up here. A Python it
# had to install just now isn't on the PATH of anything already running, and
# the Microsoft Store's "python" stand-in can answer instead of the real one,
# so the task puts the recorded one first. Without the file (the laptop
# install) plain "python" is used, as before.
$pythonFile = Join-Path $PSScriptRoot 'python_path.txt'
if (Test-Path $pythonFile) {
    $env:PATH = (Split-Path -Parent (Get-Content $pythonFile -Raw).Trim()) + ';' + $env:PATH
}

$yesterday = (Get-Date).AddDays(-1)
$to = $yesterday.ToString('yyyy-MM-dd')
$marker = Join-Path $PSScriptRoot 'last_full_resync.txt'
$fullResync = -not (Test-Path $marker) -or ((Get-Date) - (Get-Item $marker).LastWriteTime).TotalDays -ge 7
if ($fullResync) {
    $fyYear = if ($yesterday.Month -ge 4) { $yesterday.Year } else { $yesterday.Year - 1 }
    $from = "$fyYear-04-01"
    $kind = 'full financial year'
} else {
    $from = (Get-Date).AddDays(-7).ToString('yyyy-MM-dd')
    $kind = 'last 7 days'
}
# New file name: the old sync_log.txt was written by PowerShell's own *>>
# redirect, which in Windows PowerShell 5.1 is UTF-16, and appending plain
# text to it below would come out garbled.
$logFile = Join-Path $PSScriptRoot 'daily_sync_log.txt'
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

Add-Content -Path $logFile -Encoding ASCII -Value "----- Scheduled run $timestamp, syncing $from to $to ($kind) -----"
# Through cmd rather than PowerShell's *>> redirect: PowerShell 5.1 wraps
# every line a native program writes to stderr -- which is where Python's
# logging goes, INFO lines included -- in a NativeCommandError, so the old
# log read as a wall of errors even on a run that worked.
cmd /c "python sync_tally.py --backfill-from $from --backfill-to $to >> `"$logFile`" 2>&1"
$exitCode = $LASTEXITCODE
Add-Content -Path $logFile -Encoding ASCII -Value "----- Exit code: $exitCode -----"
if ($fullResync -and $exitCode -eq 0) {
    Set-Content -Path $marker -Encoding ASCII -Value $timestamp
}
