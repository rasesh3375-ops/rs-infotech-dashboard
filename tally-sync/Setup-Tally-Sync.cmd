<# : Windows cmd runs the next few lines; PowerShell skips them as a comment.
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -Command "$SetupFile='%~f0'; Invoke-Expression ([IO.File]::ReadAllText($SetupFile))"
echo.
pause
exit /b
#>

# ---------------------------------------------------------------------------
# R.S. Infotech dashboard -- one-file setup for the Tally sync on an office PC
#
# Double-click this file on the PC where Tally is used every day, logged in
# as the person who uses Tally there. Put the Firebase key in the same
# folder first: service-account.json from the laptop, or a new key
# downloaded from the Firebase console (Project settings > Service
# accounts > Generate new private key), which is used as it's named. Running it again later is safe: it updates the
# scripts to the latest version and re-checks everything.
#
# What it does:
#   1. Copies the Firebase key from next to this file.
#   2. Installs Python for this Windows user if there isn't one.
#   3. Downloads the sync scripts from GitHub into C:\rs-infotech-sync.
#   4. Installs the two Python packages the sync needs.
#   5. Creates the daily 10 AM scheduled task for this user, and makes this
#      the main sync PC (another PC running the sync becomes the backup).
#   6. Checks the key against the database and that Tally answers.
# No administrator rights are needed for any of it.
# ---------------------------------------------------------------------------

# 'Continue', with -ErrorAction Stop on each step that must not fail quietly:
# under 'Stop', Windows PowerShell 5.1 turns every line Python writes to
# stderr -- its normal log lines included -- into a fatal error.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$TaskName   = 'RS Infotech Daily Tally Sync'
$TaskTime   = '10:00'
$RawBase    = 'https://raw.githubusercontent.com/rasesh3375-ops/rs-infotech-dashboard/main/tally-sync'
$PythonUrl  = 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe'
$SourceDir  = Split-Path -Parent $SetupFile

function Step($text)  { Write-Host ''; Write-Host "== $text" -ForegroundColor Cyan }
function Ok($text)    { Write-Host "   OK   $text" -ForegroundColor Green }
function Warn($text)  { Write-Host "   !!   $text" -ForegroundColor Yellow }
function Fail($text)  {
    Write-Host ''
    Write-Host "   SETUP STOPPED: $text" -ForegroundColor Red
    Write-Host '   Nothing else was changed. Fix the above and run this file again.' -ForegroundColor Red
    exit 1
}

Write-Host 'R.S. Infotech dashboard - Tally sync setup' -ForegroundColor White
Write-Host "Windows user: $env:USERDOMAIN\$env:USERNAME"

# --- 1. Firebase key --------------------------------------------------------
Step 'Checking the Firebase key (service-account.json)'
$keySource = Join-Path $SourceDir 'service-account.json'
if (-not (Test-Path $keySource)) {
    # A key downloaded fresh keeps its own name -- from the Firebase console
    # rs-infotech-dashboard-firebase-adminsdk-....json, from Google Cloud's
    # restricted sync account rs-infotech-dashboard-1a2b3c....json -- and
    # renaming it goes wrong when Windows hides extensions
    # (service-account.json.json), so the newest file next to this one that
    # is a key for this project is used as it is, whatever it's called.
    $found = Get-ChildItem -Path $SourceDir -Filter '*.json' -ErrorAction SilentlyContinue |
             Sort-Object LastWriteTime -Descending | Where-Object {
                 try { $k = Get-Content $_.FullName -Raw | ConvertFrom-Json; $k.type -eq 'service_account' -and $k.project_id -eq 'rs-infotech-dashboard' }
                 catch { $false } } | Select-Object -First 1
    if ($found) { $keySource = $found.FullName }
}
if (-not (Test-Path $keySource)) {
    Fail "No Firebase key was found next to this file ($SourceDir). In the Firebase console open Project settings > Service accounts > Generate new private key, and save the file in the same folder as this setup file."
}
try { $key = Get-Content $keySource -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { Fail 'service-account.json is not a valid key file (it could not be read as JSON).' }
if ($key.type -ne 'service_account' -or $key.project_id -ne 'rs-infotech-dashboard') {
    Fail "service-account.json is not the key for the rs-infotech-dashboard project (it says project '$($key.project_id)')."
}
Ok 'Key found and it is for rs-infotech-dashboard.'

# --- 2. Python --------------------------------------------------------------
function Find-Python {
    foreach ($cmd in @('py', 'python')) {
        if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { continue }
        try {
            $pyArgs = if ($cmd -eq 'py') { @('-3', '-c', 'import sys;print(sys.executable)') } else { @('-c', 'import sys;print(sys.executable)') }
            $p = (& $cmd @pyArgs 2>$null | Select-Object -Last 1)
            # The Microsoft Store's "python" stand-in lives in WindowsApps and only opens the Store.
            if ($p -and (Test-Path $p) -and $p -notmatch 'WindowsApps') { return $p.Trim() }
        } catch { }
    }
    $local = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($local) { return $local.FullName }
    return $null
}

Step 'Checking for Python'
$python = Find-Python
if (-not $python) {
    Write-Host '   Python is not installed for this user - downloading it (about 25 MB)...'
    $installer = Join-Path $env:TEMP 'python-3.12.7-amd64.exe'
    try { Invoke-WebRequest -Uri $PythonUrl -OutFile $installer -UseBasicParsing -ErrorAction Stop } catch { Fail "Could not download Python from python.org ($($_.Exception.Message)). Check the internet connection." }
    Write-Host '   Installing Python quietly (1-2 minutes)...'
    $p = Start-Process -FilePath $installer -ArgumentList '/quiet InstallAllUsers=0 PrependPath=1 Include_test=0' -Wait -PassThru
    if ($p.ExitCode -ne 0) { Fail "The Python installer failed (exit code $($p.ExitCode))." }
    $python = Find-Python
    if (-not $python) { Fail 'Python was installed but could not be found afterwards.' }
}
Ok "Python: $python"

# --- 3. Sync scripts --------------------------------------------------------
Step 'Downloading the sync scripts from GitHub'
$installRoot = 'C:\rs-infotech-sync'
try { New-Item -ItemType Directory -Force -Path $installRoot -ErrorAction Stop | Out-Null }
catch {
    # Some PCs don't let ordinary users create folders on C:\ itself.
    try {
        $installRoot = Join-Path $env:LOCALAPPDATA 'rs-infotech-sync' -ErrorAction Stop
        New-Item -ItemType Directory -Force -Path $installRoot -ErrorAction Stop | Out-Null
    } catch { Fail "Could not create a folder for the sync ($($_.Exception.Message))." }
}
$dir = Join-Path $installRoot 'tally-sync'
try { New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null } catch { Fail "Could not create $dir ($($_.Exception.Message))." }

# Each file must contain a line only the current version has, so a stale or
# half-downloaded copy is caught here instead of failing at 10 AM tomorrow.
$files = [ordered]@{
    'sync_tally.py'      = 'def run_check'
    'run_daily_sync.ps1' = 'python_path.txt'
    'requirements.txt'   = 'firebase-admin'
}
foreach ($name in $files.Keys) {
    $tmp = Join-Path $dir "$name.download"
    try { Invoke-WebRequest -Uri "$RawBase/$name" -OutFile $tmp -UseBasicParsing -ErrorAction Stop } catch { Fail "Could not download $name from GitHub ($($_.Exception.Message))." }
    if (-not (Select-String -Path $tmp -SimpleMatch $files[$name] -Quiet)) { Remove-Item $tmp; Fail "The downloaded $name is not the expected version." }
    Move-Item -Force $tmp (Join-Path $dir $name)
}
Copy-Item -Force $keySource (Join-Path $dir 'service-account.json')
Set-Content -Path (Join-Path $dir 'python_path.txt') -Value $python -Encoding ASCII
# This PC is the main sync PC; another PC running the sync (the laptop)
# becomes the backup and only syncs while this one is quiet.
Set-Content -Path (Join-Path $dir 'sync_role.txt') -Value 'primary' -Encoding ASCII
Ok "Installed in $dir"

# --- 4. Python packages -----------------------------------------------------
Step 'Installing the Python packages (requests, firebase-admin) - 1-2 minutes'
& $python -m pip install --quiet --disable-pip-version-check --upgrade -r (Join-Path $dir 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail 'Installing the Python packages failed. Check the internet connection and run this file again.' }
Ok 'Packages installed.'

# --- 5. Scheduled task ------------------------------------------------------
Step "Creating the daily $TaskTime sync task"
$action    = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument "-WindowStyle Hidden -NoProfile -ExecutionPolicy Bypass -File `"$dir\run_daily_sync.ps1`""
$trigger   = New-ScheduledTaskTrigger -Daily -At $TaskTime
# Runs on battery, and if the PC was off or asleep at the set time it runs as
# soon as this user is logged on again.
$settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
try { Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force -ErrorAction Stop | Out-Null }
catch { Fail "Could not create the scheduled task ($($_.Exception.Message))." }
Ok "Task '$TaskName' runs every day at $TaskTime while this user is logged on."

# --- 6. Checks --------------------------------------------------------------
# sync_tally.py --check tests the key and Tally through the sync's own code,
# so it passes exactly when the 10 AM sync would work.
Step 'Checking the key and Tally (Tally must be open with R. S. Infotech loaded)'
Push-Location $dir
$out = & $python sync_tally.py --check 2>&1 | ForEach-Object { "$_" }
Pop-Location
$checks = $out | Where-Object { $_ -match '^CHECK ' }
$problems = 0
$db = $checks | Where-Object { $_ -match '^CHECK database:' }
if ($db -match ': OK') { Ok 'The key works - the dashboard database answered.' }
else { $problems++; Warn "The database did not accept the key: $db" }
$tally = $checks | Where-Object { $_ -match '^CHECK tally:' }
if ($tally -match ': OK') { Ok ($tally -replace '^CHECK tally: OK -- ', 'Tally answers and ') }
elseif ($tally -match 'Could not reach Tally|did not respond') {
    $problems++
    Warn 'Tally did not answer on this PC. Make sure Tally is open with R. S. Infotech loaded, then in Tally:'
    Warn '   F1 (Help) > Settings > Connectivity > "TallyPrime acts as" = Both, Port = 9000'
    Warn '   Restart Tally after changing it, then run this setup file again.'
}
else { $problems++; Warn "Tally answered but R. S. Infotech could not be read: $tally" }
if (-not $checks) { $problems++; Warn "The check itself did not run: $($out | Select-Object -Last 3)" }

# --- Done -------------------------------------------------------------------
Write-Host ''
if ($problems -eq 0) {
    Write-Host 'ALL DONE. The sync is set up and every check passed.' -ForegroundColor Green
    Write-Host ''
    $answer = Read-Host 'Run the first sync now? It covers the whole year and takes about 15 minutes, in the background with no window - the dashboard fills in as it goes. (Y/N)'
    if ($answer -match '^[Yy]') { Start-ScheduledTask -TaskName $TaskName; Ok 'First sync started. It ends with "Exit code: 0" in the log file below.' }
    else { Write-Host '   It will run by itself at the next scheduled time.' }
    Write-Host ''
    Write-Host "Log file: $dir\daily_sync_log.txt"
} else {
    Write-Host "SET UP, BUT $problems CHECK(S) FAILED - see the yellow lines above. Fix them and run this file again." -ForegroundColor Yellow
}
