"""
Starts run_daily_sync.ps1 with no window at all. The daily scheduled task
runs this with pythonw.exe instead of running powershell.exe itself.

Why: the task used to start "powershell.exe -WindowStyle Hidden", and on
Windows 11, where Windows Terminal opens console programs, a window still
came up in front of whatever the owner was doing -- PowerShell can only
hide its window after it has been given one. pythonw.exe is a windowed
program with no console, so starting it opens nothing, and it starts
PowerShell with CREATE_NO_WINDOW, which gives PowerShell (and the python
and cmd it runs in turn) a console that has no window from the start.

run_daily_sync.ps1 switches the task over to this file by itself (see
"No window for the daily run" there), so no PC needs setting up again.
"""

import os
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

here = os.path.dirname(os.path.abspath(__file__))
sys.exit(subprocess.run(
    ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", os.path.join(here, "run_daily_sync.ps1")],
    cwd=here, creationflags=CREATE_NO_WINDOW).returncode)
