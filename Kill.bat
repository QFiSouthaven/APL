@echo off
REM Kill.bat — free the APL umbrella ports.
REM
REM Finds any process LISTENING on 8765 (prompt-enhancer), 8766 (round-robin),
REM or 8767 (development) and force-kills it. Useful when:
REM   - A previous Start.bat run left orphaned children behind
REM   - You ran a sibling manually and forgot to Ctrl-C
REM   - Start.bat reports "address already in use" / WinError 10048
REM
REM Does NOT touch port 1234 (LM Studio) — that's the LLM backend; you
REM probably want to keep it running.
REM
REM Lists what was found before killing. Pause at the end so the cmd
REM window stays open after a double-click.

setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ======================================================================
echo   APL umbrella - port cleanup
echo ======================================================================
echo.
echo Scanning ports 8765, 8766, 8767 for LISTENING processes...
echo.

set /a FOUND=0
set /a KILLED=0

for %%P in (8765 8766 8767) do (
    for /f "tokens=5" %%I in ('netstat -ano ^| findstr ":%%P " ^| findstr LISTENING') do (
        set /a FOUND+=1
        echo [port %%P] LISTENING by PID %%I
        REM /T = also kill child processes; /F = force
        taskkill /F /T /PID %%I >NUL 2>&1
        if !errorlevel! equ 0 (
            echo            killed.
            set /a KILLED+=1
        ) else (
            echo            could not kill ^(already exited or access denied^).
        )
    )
)

echo.
if !FOUND! equ 0 (
    echo All clear. No processes squatting on umbrella ports.
) else (
    echo Found !FOUND! squatter^(s^); killed !KILLED!.
    if !KILLED! lss !FOUND! (
        echo.
        echo Some kills failed. Try one of:
        echo   - Re-run this script as Administrator
        echo   - Reboot
    )
)
echo.

REM Verify ports are now free.
set /a STILL=0
for %%P in (8765 8766 8767) do (
    netstat -ano | findstr ":%%P " | findstr LISTENING >NUL
    if !errorlevel! equ 0 (
        echo [WARN] port %%P still in use.
        set /a STILL+=1
    )
)
if !STILL! equ 0 (
    echo Verified: all three umbrella ports are free.
)
echo.
pause

endlocal
