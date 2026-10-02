@echo off
setlocal

set REPO_PATH=C:\weltrade-bot
set BRANCH_NAME=test-branch

cd /d "%REPO_PATH%"

echo [%date% %time%] Checking for updates... >> sync_log.txt

git fetch origin

for /f %%i in ('git rev-parse HEAD') do set OLDREV=%%i
for /f %%i in ('git rev-parse origin/%BRANCH_NAME%') do set REMOTEREV=%%i

if "%OLDREV%"=="%REMOTEREV%" (
    echo [%date% %time%] No changes. >> sync_log.txt
    exit /b 0
)

echo [%date% %time%] New commits found. Pulling... >> sync_log.txt
git reset --hard origin/%BRANCH_NAME%

endlocal