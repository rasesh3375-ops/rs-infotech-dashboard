# Wrapper for Windows Task Scheduler -- re-syncs the last 7 days (up to and
# including yesterday) every morning.
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
# document is rebuilt whole from Tally, and delivery challans are merged
# without touching their billed flag.
#
# Requires Tally Prime open with the company loaded on THIS PC -- this only
# talks to Tally over localhost:9000, it does not launch Tally. If Tally
# isn't open, or can't read its shared data folder, the sync stops with an
# error before writing anything, and the next morning's run covers the day.

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$from = (Get-Date).AddDays(-7).ToString('yyyy-MM-dd')
$to = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
# New file name: the old sync_log.txt was written by PowerShell's own *>>
# redirect, which in Windows PowerShell 5.1 is UTF-16, and appending plain
# text to it below would come out garbled.
$logFile = Join-Path $PSScriptRoot 'daily_sync_log.txt'
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

Add-Content -Path $logFile -Encoding ASCII -Value "----- Scheduled run $timestamp, syncing $from to $to -----"
# Through cmd rather than PowerShell's *>> redirect: PowerShell 5.1 wraps
# every line a native program writes to stderr -- which is where Python's
# logging goes, INFO lines included -- in a NativeCommandError, so the old
# log read as a wall of errors even on a run that worked.
cmd /c "python sync_tally.py --backfill-from $from --backfill-to $to >> `"$logFile`" 2>&1"
Add-Content -Path $logFile -Encoding ASCII -Value "----- Exit code: $LASTEXITCODE -----"
