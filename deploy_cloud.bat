@echo off
setlocal
cd /d "%~dp0"

set "REPO=%~1"
if "%REPO%"=="" set /p "REPO=Paste your GitHub repo URL (https://github.com/user/repo.git): "
if "%REPO%"=="" (
  echo No repo URL given, aborted.
  pause
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_cloud.ps1" -RepoUrl "%REPO%"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Deploy FAILED, exit=%RC%
pause
exit /b %RC%
