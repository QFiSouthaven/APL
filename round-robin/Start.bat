@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo [Round Robin] Starting...

where python >nul 2>&1
if errorlevel 1 goto :nopython

if not exist ".venv\Scripts\python.exe" goto :makevenv
goto :checkdeps

:makevenv
echo [Round Robin] First run - creating virtual environment...
python -m venv .venv
if errorlevel 1 goto :venvfail

:checkdeps
".venv\Scripts\pip.exe" show round-robin >nul 2>&1
if errorlevel 1 goto :install
goto :launch

:install
echo [Round Robin] Installing dependencies (this may take a minute)...
".venv\Scripts\pip.exe" install -e ".[dev]"
if errorlevel 1 goto :pipfail

:launch
echo [Round Robin] Launching desktop window...
".venv\Scripts\python.exe" app.py
if errorlevel 1 goto :appfail
goto :done

:nopython
echo.
echo [Round Robin] ERROR: 'python' was not found on PATH.
echo Install Python 3.11 or newer from https://www.python.org/downloads/
echo Make sure to check "Add Python to PATH" during installation.
goto :end

:venvfail
echo.
echo [Round Robin] ERROR: Failed to create virtual environment.
goto :end

:pipfail
echo.
echo [Round Robin] ERROR: pip install failed.
echo Try running manually: .venv\Scripts\pip install -e ".[dev]"
goto :end

:appfail
echo.
echo [Round Robin] App exited with an error. Common causes:
echo   - LM Studio is not running on port 1234
echo   - no model is loaded in LM Studio
echo   - WebView2 runtime is missing (download from Microsoft)
goto :end

:done
echo [Round Robin] Window closed normally.

:end
echo.
pause
endlocal
