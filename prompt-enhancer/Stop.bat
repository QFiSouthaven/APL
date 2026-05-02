@echo off
REM Stop.bat — terminate any process listening on the Studio port (8765).

setlocal enabledelayedexpansion

set FOUND=0
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 " ^| findstr LISTENING') do (
    echo Killing PID %%a
    taskkill /F /PID %%a 2>NUL
    set FOUND=1
)

if "!FOUND!"=="0" (
    echo Nothing listening on port 8765.
) else (
    echo Done.
)

endlocal
