@echo off
setlocal
cd /d "%~dp0"
if not exist "logs" mkdir "logs"
for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "TODAY=%%i"
set "LOG=logs\checkin_%TODAY%.log"
echo.>>"%LOG%"
echo ===== start %DATE% %TIME% =====>>"%LOG%"
"D:\Python313\python.exe" -u "checkin.py">>"%LOG%" 2>&1
set "RC=%ERRORLEVEL%"
echo ===== end   %DATE% %TIME% exit=%RC% =====>>"%LOG%"
exit /b %RC%
