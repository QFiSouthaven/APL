@echo off
REM Setup.bat — first-time provisioning for the APL umbrella.
REM
REM Creates a .venv for each sibling (prompt-enhancer, round-robin, development),
REM pip-installs each editable, cross-installs prompt-enhancer into the other two
REM siblings' venvs so cross-discovery imports work, and bootstraps services.toml.
REM
REM Idempotent: safe to re-run after a checkout refresh or a `pip` upgrade. Only
REM creates venvs that don't exist; pip skips already-satisfied installs.
REM
REM After this completes, run Start.bat to boot the full umbrella.
REM
REM On any failure, the script PAUSES so the error is visible. Press any key
REM to close once you've read the message.

setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

echo ======================================================================
echo   APL umbrella setup
echo   (cwd: %CD%)
echo ======================================================================

REM --- 0. Verify python on PATH ---------------------------------------------
echo.
echo [0/4] python --version
where python >NUL 2>&1
if errorlevel 1 (
    echo [ERROR] python not on PATH. Install Python 3.10+ and re-run.
    pause
    exit /b 1
)
python --version
python -c "import sys; assert sys.version_info >= (3, 10), sys.version"
if errorlevel 1 (
    echo [ERROR] Python 3.10+ required.
    pause
    exit /b 1
)

REM --- 1. prompt-enhancer ---------------------------------------------------
echo.
echo [1/4] prompt-enhancer
if not exist "prompt-enhancer\.venv\Scripts\activate.bat" (
    echo   creating venv at prompt-enhancer\.venv ...
    python -m venv "prompt-enhancer\.venv"
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        pause
        exit /b 1
    )
) else (
    echo   venv exists, skipping creation.
)
echo   activating prompt-enhancer\.venv ...
call "prompt-enhancer\.venv\Scripts\activate.bat"
echo   pip install -e .\prompt-enhancer[ui,dev]
pip install -e ".\prompt-enhancer[ui,dev]" --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] prompt-enhancer install failed.
    pause
    exit /b 1
)
call "prompt-enhancer\.venv\Scripts\deactivate.bat" >NUL 2>&1

REM --- 2. round-robin -------------------------------------------------------
echo.
echo [2/4] round-robin
if not exist "round-robin\.venv\Scripts\activate.bat" (
    echo   creating venv at round-robin\.venv ...
    python -m venv "round-robin\.venv"
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        pause
        exit /b 1
    )
) else (
    echo   venv exists, skipping creation.
)
echo   activating round-robin\.venv ...
call "round-robin\.venv\Scripts\activate.bat"
echo   pip install -e .\round-robin[dev]
pip install -e ".\round-robin[dev]" --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] round-robin install failed.
    pause
    exit /b 1
)
echo   pip install -e .\prompt-enhancer (cross-install)
pip install -e ".\prompt-enhancer" --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] cross-install of prompt-enhancer into round-robin failed.
    pause
    exit /b 1
)
call "round-robin\.venv\Scripts\deactivate.bat" >NUL 2>&1

REM --- 3. development -------------------------------------------------------
echo.
echo [3/4] development
if not exist "development\.venv\Scripts\activate.bat" (
    echo   creating venv at development\.venv ...
    python -m venv "development\.venv"
    if errorlevel 1 (
        echo [ERROR] venv creation failed.
        pause
        exit /b 1
    )
) else (
    echo   venv exists, skipping creation.
)
echo   activating development\.venv ...
call "development\.venv\Scripts\activate.bat"
echo   pip install -e .\development[dev]
pip install -e ".\development[dev]" --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] development install failed.
    pause
    exit /b 1
)
echo   pip install -e .\prompt-enhancer (cross-install)
pip install -e ".\prompt-enhancer" --disable-pip-version-check
if errorlevel 1 (
    echo [ERROR] cross-install of prompt-enhancer into development failed.
    pause
    exit /b 1
)
call "development\.venv\Scripts\deactivate.bat" >NUL 2>&1

REM --- 4. Bootstrap services.toml -------------------------------------------
echo.
echo [4/4] services.toml bootstrap
call "prompt-enhancer\.venv\Scripts\activate.bat"
python -m enhancer.cli.main services init
python -m enhancer.cli.main services path
call "prompt-enhancer\.venv\Scripts\deactivate.bat" >NUL 2>&1

echo.
echo ======================================================================
echo   Setup complete. Next steps:
echo     - Start.bat            (boot the full umbrella)
echo     - prompt-enhancer\Start.bat   (single-sibling Studio only)
echo ======================================================================
echo.
pause

endlocal
