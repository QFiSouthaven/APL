@echo off
REM Start.bat — boot the full APL umbrella (prompt-enhancer + round-robin + development).
REM
REM Each sibling launches in its own subprocess via lab/launch.py, blocks until
REM each /api/health returns 200, and shows a live banner. Ctrl+C in this
REM window stops the whole umbrella cleanly.
REM
REM First time? Run Setup.bat first to create per-sibling venvs.
REM
REM For Studio-only (no round-robin / development), use:
REM     prompt-enhancer\Start.bat

setlocal
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

REM --- Pre-flight: each sibling needs a venv -------------------------------
if not exist "prompt-enhancer\.venv\Scripts\python.exe" goto :need_setup
if not exist "round-robin\.venv\Scripts\python.exe"     goto :need_setup
if not exist "development\.venv\Scripts\python.exe"     goto :need_setup
goto :launch

:need_setup
echo.
echo [ERROR] One or more sibling venvs are missing.
echo Run Setup.bat first to provision them.
echo.
pause
exit /b 1

:launch
REM --- Activate prompt-enhancer's venv (the orchestrator's runtime) --------
REM    lab/launch.py spawns each sibling using its OWN .venv\Scripts\python.exe
REM    via absolute paths, so activation here only affects this orchestrator
REM    process. Each sibling still runs under its own venv. Sub-venvs are not
REM    "nested" — they're sibling subprocesses with their own interpreters.
call "prompt-enhancer\.venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] failed to activate prompt-enhancer\.venv. Re-run Setup.bat.
    pause
    exit /b 1
)

REM --- Idempotent services.toml bootstrap (silent) -------------------------
python -m enhancer.cli.main services init >NUL 2>&1

REM --- Quick LM Studio reachability hint (non-fatal) -----------------------
python -c "import httpx; httpx.get('http://127.0.0.1:1234/v1/models', timeout=3.0).raise_for_status()" 2>NUL
if errorlevel 1 (
    echo [WARNING] LM Studio not reachable at 127.0.0.1:1234.
    echo The siblings will boot but pipeline runs will fail until LM Studio is up.
    echo.
)

REM --- Boot the umbrella ---------------------------------------------------
echo Launching APL umbrella via lab\launch.py
echo   prompt-enhancer  http://127.0.0.1:8765
echo   round-robin      http://127.0.0.1:8766
echo   development      http://127.0.0.1:8767
echo (Ctrl+C in this window to stop all three)
echo.

python "lab\launch.py"

REM Pause after launcher exits so the user sees any error message before
REM the cmd window closes. Normal Ctrl-C shutdown also lands here.
echo.
echo [launch.py exited with code %ERRORLEVEL%]
pause

endlocal
