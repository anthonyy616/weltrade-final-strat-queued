@echo off

:: Set working directory to the folder containing this script
cd /d "%~dp0"


setlocal enabledelayedexpansion

:: Match this to BOT_PORT in your .env / main.py (default is 800)
set PORT=800
set RULE_NAME=MT5_Bot_Server

:: --- Check for Administrator privileges ---
net session >nul 2>&1

if %errorLevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo.
echo Configuring firewall for port %PORT% ...
echo.

:: Read the port from .env if it exists, otherwise default to 800
set BOT_PORT=800

if exist ".env" (
    for /f "tokens=2 delims==" %%a in ('findstr /i "BOT_PORT" .env') do set BOT_PORT=%%a
)

:: Ensure BOT_PORT has a value
if "%BOT_PORT%"=="" set BOT_PORT=800
echo  Using port: %BOT_PORT%

echo.
echo  Opening port %BOT_PORT% in Windows Firewall...
echo.

:: Remove any existing rules with the same name to avoid duplicates
netsh advfirewall firewall delete rule name="Weltrade Bot" >nul 2>&1

:: Add inbound rule (allows incoming connections)
netsh advfirewall firewall add rule ^
    name="Weltrade Bot" ^
    dir=in ^
    action=allow ^
    protocol=TCP ^
    localport=%BOT_PORT% ^
    description="Allows incoming connections to the Weltrade trading bot"

if %errorLevel% equ 0 (
    echo.
    echo  SUCCESS - Port %BOT_PORT% is now open!
    echo.
) else (
    echo.
    echo  ERROR: Could not open the port.
    echo.
    pause
    exit /b 1
)

:: Get the machine's public-facing IP to show the user their access URL
echo  Finding your IP address...
echo.

set PUBLIC_IP=
for /f "usebackq delims=" %%i in (`powershell -NoProfile -Command "(Invoke-RestMethod -Uri 'https://api.ipify.org').Trim()" 2^>nul`) do set PUBLIC_IP=%%i

if "%PUBLIC_IP%"=="" (
    :: Fallback to local IP if internet check fails
    for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4"') do (
        set PUBLIC_IP=%%a
        goto :found_ip
    )
)
:found_ip

:: Remove spaces if defined
if defined PUBLIC_IP set PUBLIC_IP=%PUBLIC_IP: =%

echo.
echo ============================================================
echo   FIREWALL CONFIGURED SUCCESSFULLY
echo ============================================================
echo.
if defined LOCAL_IP (
    echo   Same Wi-Fi / LAN access:
    echo     http://%LOCAL_IP%:%PORT%
    echo.
)
if defined PUBLIC_IP (
    echo   Internet access ^(only works once router port forwarding
    echo   is set up - see note above^):
    echo     http://%PUBLIC_IP%:%PORT%
    echo.
) else (
    echo   Could not auto-detect your public IP.
    echo   Look it up at whatismyip.com, then use:
    echo     http://YOUR_PUBLIC_IP:%PORT%
    echo.
)
echo   Note: the public link may stop working if your ISP changes
echo   your public IP. Consider a dynamic DNS service if so.
echo ============================================================
echo.
echo   Save this address - this is what you and
echo   your users will type into their browser.
echo.
echo  =========================================
echo.
pause
