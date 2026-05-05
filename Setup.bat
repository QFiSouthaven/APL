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

setlocal
cd /d "%~dp0"

set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"

echo ======================================================================
echo   APL umbrella setup
echo ======================================================================

REM --- 0. Verify python on PATH ---------------------------------------------
where python >NUL 2>&1
if errorlevel 1 (
    echo [ERROR] python not on PATH. Install Python 3.10+ and re-run.
    exit /b 1
)
python -c "import sys; assert sys.version_info >= (3, 10), sys.version" 2>NUL
if errorlevel 1 (
    echo [ERROR] Python 3.10+ required.
    python --version
    exit /b 1
)

REM --- 1. prompt-enhancer ---------------------------------------------------
echo.
echo [1/4] prompt-enhancer
if not exist "prompt-enhancer\.venv\Scripts\activate.bat" (
    echo   creating venv...
    python -m venv "prompt-enhancer\.venv"
    if errorlevel 1 ( echo [ERROR] venv creation failed. & exit /b 1 )
)
call "prompt-enhancer\.venv\Scripts\activate.bat"
echo   pip install -e ".[ui,dev]"
pip install -e "prompt-enhancer[ui,dev]" --quiet --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] prompt-enhancer install failed. & exit /b 1 )
call "prompt-enhancer\.venv\Scripts\deactivate.bat" >NUL 2>&1

REM --- 2. round-robin -------------------------------------------------------
echo.
echo [2/4] round-robin
if not exist "round-robin\.venv\Scripts\activate.bat" (
    echo   creating venv...
    python -m venv "round-robin\.venv"
    if errorlevel 1 ( echo [ERROR] venv creation failed. & exit /b 1 )
)
call "round-robin\.venv\Scripts\activate.bat"
echo   pip install -e ".[dev]" + prompt-enhancer
pip install -e "round-robin[dev]" --quiet --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] round-robin install failed. & exit /b 1 )
pip install -e "prompt-enhancer" --quiet --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] cross-install of prompt-enhancer failed. & exit /b 1 )
call "round-robin\.venv\Scripts\deactivate.bat" >NUL 2>&1

REM --- 3. development -------------------------------------------------------
echo.
echo [3/4] development
if not exist "development\.venv\Scripts\activate.bat" (
    echo   creating venv...
    python -m venv "development\.venv"
    if errorlevel 1 ( echo [ERROR] venv creation failed. & exit /b 1 )
)
call "development\.venv\Scripts\activate.bat"
echo   pip install -e ".[dev]" + prompt-enhancer
pip install -e "development[dev]" --quiet --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] development install failed. & exit /b 1 )
pip install -e "prompt-enhancer" --quiet --disable-pip-version-check
if errorlevel 1 ( echo [ERROR] cross-install of prompt-enhancer failed. & exit /b 1 )
call "development\.venv\Scripts\deactivate.bat" >NUL 2>&1

REM --- 4. Bootstrap services.toml -------------------------------------------
echo.
echo [4/4] services.toml bootstrap
call "prompt-enhancer\.venv\Scripts\activate.bat"
python -m enhancer.cli.main services init >NUL 2>&1
python -m enhancer.cli.main services path
call "prompt-enhancer\.venv\Scripts\deactivate.bat" >NUL 2>&1

echo.
echo ======================================================================
echo   Setup complete. Next steps:
echo     - Start.bat            (boot the full umbrella)
echo     - prompt-enhancer\Start.bat   (single-sibling Studio only)
echo ======================================================================

endlocal
