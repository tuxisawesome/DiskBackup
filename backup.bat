@echo off
rem Starts DiskBackup. Double-click this file, or run it from a terminal.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m diskbackup %*
) else (
    python -m diskbackup %*
)
if errorlevel 1 pause
