# Wrapper for Windows Task Scheduler -- syncs YESTERDAY's Tally figures
# every morning, once the previous day's vouchers have actually been
# entered. sync_tally.py with no --date always syncs TODAY, which is the
# wrong day for a morning run: vouchers get entered same-day here, so a
# 10 AM run without an explicit date would only catch whatever's been
# typed into Tally by 10 AM and miss the rest of the day entirely. This
# always passes the previous calendar day instead.
#
# Requires Tally Prime already open with the company loaded on THIS PC --
# this only talks to Tally over its local HTTP/XML gateway (localhost:9000),
# it does not launch Tally itself. If Tally isn't open when the scheduled
# task fires, the sync fails and that's logged below; nothing is written to
# Firestore for that day. Re-run by hand once Tally is open:
#   python sync_tally.py --date YYYY-MM-DD

$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$yesterday = (Get-Date).AddDays(-1).ToString('yyyy-MM-dd')
$logFile = Join-Path $PSScriptRoot 'sync_log.txt'
$timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'

Add-Content -Path $logFile -Value "----- Scheduled run $timestamp, syncing $yesterday -----"
python sync_tally.py --date $yesterday --verbose *>> $logFile
Add-Content -Path $logFile -Value "----- Exit code: $LASTEXITCODE -----"
