@echo off
REM Start.bat — launch the Prompt Enhancer Desktop Studio (single sibling).
REM Idempotent: re-creates venv if missing, re-installs deps if needed,
REM warns if LM Studio is unreachable but lets the user proceed.
REM
REM For the FULL umbrella (prompt-enhancer + round-robin + development
REM with cross-discovery), use:    python ..\lab\launch.py
REM See ..\lab\README.md for details.

setlocal
cd /d "%~dp0"

REM Force UTF-8 stdout so streamed LLM glyphs (smart quotes, em dashes, etc.)
REM don't crash the Windows cp1252 console.
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1

REM 1. Create venv on first run.
if not exist ".venv\Scripts\activate.bat" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo [ERROR] python not on PATH. Install Python 3.10+ and retry.
        exit /b 1
    )
)
call ".venv\Scripts\activate.bat"

REM 2. Install/refresh package + UI extras (pip skips when already satisfied).
echo Checking dependencies...
pip install -e ".[ui]" --quiet --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] pip install failed. Check the trace above.
    exit /b 1
)

REM 3. First-run bootstrap: write services.toml if missing so the user
REM    can discover/customize peer URLs without hunting for the path.
REM    Silent on success; failure is non-fatal (just continues).
python -m enhancer.cli.main services init >NUL 2>&1

REM 4. Sanity check: LM Studio reachable.
python -c "import httpx; httpx.get('http://127.0.0.1:1234/v1/models', timeout=3.0).raise_for_status()" 2>NUL
if errorlevel 1 (
    echo.
    echo [WARNING] LM Studio not reachable at 127.0.0.1:1234.
    echo Open LM Studio, load a model, start the Local Server.
    echo Press any key to launch anyway, or Ctrl+C to abort.
    pause >NUL
)

REM 5. Launch NiceGUI Desktop Studio.
echo.
echo Launching Prompt Enhancer Desktop Studio at http://127.0.0.1:8765
echo (Ctrl+C in this window to stop)
echo (For the full umbrella with round-robin + development, use ..\lab\launch.py)
echo.
python -m enhancer.cli.main ui

endlocal
