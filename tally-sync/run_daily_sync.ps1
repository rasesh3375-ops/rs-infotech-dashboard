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
# Raised whenever sync_tally.py starts storing something new on every day,
# so the next run rewrites the whole year instead of leaving the older days
# without it for up to a week. 2: Proforma Invoices kept apart from Sales,
# and Tally's before-GST Sales and Purchase figures (October 2026).
# 3: opening and closing cash in hand for the daily cash email.
# 4: the same for every bank account, for the daily bank email.
$dataFormat = 'data-format 4'
$fullResync = -not (Test-Path $marker) -or ((Get-Date) - (Get-Item $marker).LastWriteTime).TotalDays -ge 7 -or
    ((Get-Content $marker -Raw) -notmatch [regex]::Escape($dataFormat))
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

# --- Self-update from GitHub ------------------------------------------------
# Every run first fetches the current sync scripts from the (public) GitHub
# repo, so a fix reaches every PC by the next morning without anyone
# downloading a ZIP and copying files by hand -- which was the only way
# before, and went wrong more than once (an old download copied over the
# new one). A file is replaced only when it differs and passes a check:
# sync_tally.py has to compile, this script has to parse as PowerShell. If
# GitHub can't be reached or a file fails its check, the version already
# here runs unchanged. A new copy of this script itself is used from the
# next run on; PowerShell has already read this one.
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$ProgressPreference = 'SilentlyContinue'
$updateBase = 'https://raw.githubusercontent.com/rasesh3375-ops/rs-infotech-dashboard/main/tally-sync'
$updated = @(); $updateProblems = @()
foreach ($name in @('sync_tally.py', 'requirements.txt', 'run_daily_sync.ps1')) {
    $current = Join-Path $PSScriptRoot $name
    $download = Join-Path $PSScriptRoot "update-$name"
    try {
        Invoke-WebRequest -Uri "$updateBase/$name" -OutFile $download -UseBasicParsing -TimeoutSec 60 -ErrorAction Stop
    } catch {
        $updateProblems += "$name could not be downloaded"
        continue
    }
    $text = Get-Content $download -Raw
    $valid = switch ($name) {
        'sync_tally.py' {
            & python -m py_compile $download 2>$null | Out-Null
            ($LASTEXITCODE -eq 0) -and ($text -match 'def main')
        }
        'requirements.txt' { $text -match 'firebase-admin' }
        'run_daily_sync.ps1' {
            $parseErrors = $null
            [void][System.Management.Automation.Language.Parser]::ParseInput($text, [ref]$null, [ref]$parseErrors)
            ($text -match 'sync_tally\.py') -and -not $parseErrors
        }
    }
    if (-not $valid) {
        $updateProblems += "$name from GitHub failed its check"
    } elseif (-not (Test-Path $current) -or (Get-FileHash $download).Hash -ne (Get-FileHash $current).Hash) {
        Copy-Item -Force $download $current
        $updated += $name
    }
    Remove-Item -Force $download -ErrorAction SilentlyContinue
}
Remove-Item (Join-Path $PSScriptRoot '__pycache__\update-*') -Force -ErrorAction SilentlyContinue
if ($updated -contains 'requirements.txt') {
    cmd /c "python -m pip install --quiet --disable-pip-version-check -r requirements.txt >> `"$logFile`" 2>&1"
}
if ($updated) { Add-Content -Path $logFile -Encoding ASCII -Value "----- Updated from GitHub: $($updated -join ', ') -----" }
if ($updateProblems) { Add-Content -Path $logFile -Encoding ASCII -Value "----- Update skipped ($($updateProblems -join '; ')) - running the version already here -----" }

# --- Sync now listener -----------------------------------------------------
# sync_tally.py --listen answers the dashboard's Sync now button and syncs
# today hourly during office hours (see run_listener). It's installed from
# here, so every PC already running this sync gets it with nothing done by
# hand: a task that starts it at logon and every ten minutes after, in case
# it stopped (a second copy finds the first one's lock and exits at once).
# pythonw runs it without a window. Registered only when missing, or when
# the Python it points at has gone, so a running listener isn't disturbed.
$listenerTask = 'RS Infotech Tally Sync Listener'
try {
    $existing = Get-ScheduledTask -TaskName $listenerTask -ErrorAction SilentlyContinue
    if (-not $existing -or -not (Test-Path $existing.Actions[0].Execute)) {
        $pythonExe = (& python -c "import sys;print(sys.executable)" 2>$null | Select-Object -Last 1)
        if (-not $pythonExe) { throw 'python could not be run' }
        $pythonw = Join-Path (Split-Path -Parent $pythonExe.Trim()) 'pythonw.exe'
        if (-not (Test-Path $pythonw)) { $pythonw = $pythonExe.Trim() }
        $action = New-ScheduledTaskAction -Execute $pythonw -Argument "`"$PSScriptRoot\sync_tally.py`" --listen" -WorkingDirectory $PSScriptRoot
        $triggers = @(New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME")
        # Repeating with no end date has to be left implicit, and older
        # Windows versions refuse that; there the logon trigger and the
        # restart below each morning still keep it running.
        try { $triggers += New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -ErrorAction Stop } catch { }
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
        Register-ScheduledTask -TaskName $listenerTask -Action $action -Trigger $triggers -Settings $settings `
            -Principal $principal -Force -ErrorAction Stop | Out-Null
        Add-Content -Path $logFile -Encoding ASCII -Value "----- Installed the Sync now listener ($pythonw) -----"
    }
    if ((Get-ScheduledTask -TaskName $listenerTask -ErrorAction Stop).State -ne 'Running') {
        Start-ScheduledTask -TaskName $listenerTask
    }
} catch {
    Add-Content -Path $logFile -Encoding ASCII -Value "----- Could not set up the Sync now listener: $($_.Exception.Message) -----"
}

# Through cmd rather than PowerShell's *>> redirect: PowerShell 5.1 wraps
# every line a native program writes to stderr -- which is where Python's
# logging goes, INFO lines included -- in a NativeCommandError, so the old
# log read as a wall of errors even on a run that worked.
cmd /c "python sync_tally.py --backfill-from $from --backfill-to $to >> `"$logFile`" 2>&1"
$exitCode = $LASTEXITCODE
Add-Content -Path $logFile -Encoding ASCII -Value "----- Exit code: $exitCode -----"
if ($fullResync -and $exitCode -eq 0) {
    Set-Content -Path $marker -Encoding ASCII -Value "$dataFormat $timestamp"
}
