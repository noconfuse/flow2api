@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
for %%I in ("%SCRIPT_DIR%..") do set "REPO_ROOT=%%~fI"

cd /d "%REPO_ROOT%"
if not exist "tmp\host-bridge" mkdir "tmp\host-bridge"

if not defined FLOW2API_BROWSER_LAUNCH_HOST_BIND set "FLOW2API_BROWSER_LAUNCH_HOST_BIND=0.0.0.0"
if not defined FLOW2API_BROWSER_LAUNCH_HOST_PORT set "FLOW2API_BROWSER_LAUNCH_HOST_PORT=8765"

where py >nul 2>nul
if %errorlevel%==0 goto run_with_py

where python >nul 2>nul
if %errorlevel%==0 goto run_with_python

echo Python launcher not found. Install Python or add py/python to PATH.>> "tmp\host-bridge\stderr.log"
exit /b 1

:run_with_py
py -3 scripts\browser_profile_host_bridge.py >> "tmp\host-bridge\stdout.log" 2>> "tmp\host-bridge\stderr.log"
exit /b %errorlevel%

:run_with_python
python scripts\browser_profile_host_bridge.py >> "tmp\host-bridge\stdout.log" 2>> "tmp\host-bridge\stderr.log"
exit /b %errorlevel%
