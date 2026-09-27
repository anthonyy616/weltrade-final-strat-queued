@echo off
<<<<<<< HEAD
:: ============================================================
:: open_port.bat
:: Opens an inbound TCP port in Windows Defender Firewall so the
:: MT5 bot's web server (FastAPI/uvicorn) can be reached, then
:: prints the link(s) to access it.
::
:: IMPORTANT: This only controls the firewall on THIS machine.
:: To actually expose the port to the internet, you must ALSO
:: forward this port to this PC's local IP in your router's
:: admin panel. This script cannot do that part for you.
::
:: Must be run as Administrator (right-click -> Run as administrator)
:: ============================================================
=======
:: Set working directory to the folder containing this script
cd /d "%~dp0"
>>>>>>> b8a18a68cdcdd98d618176c2c3192e825c4e1120

setlocal enabledelayedexpansion

:: Match this to BOT_PORT in your .env / main.py (default is 800)
set PORT=800
set RULE_NAME=MT5_Bot_Server

:: --- Check for Administrator privileges ---
net session >nul 2>&1
<<<<<<< HEAD
if %errorlevel% neq 0 (
    echo.
    echo This script must be run as Administrator.
    echo Right-click open_port.bat and choose "Run as administrator".
    echo.
    pause
    exit /b 1
=======
if %errorLevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
>>>>>>> b8a18a68cdcdd98d618176c2c3192e825c4e1120
)

echo.
echo Configuring firewall for port %PORT% ...
echo.

<<<<<<< HEAD
:: Remove any old rule with the same name first (avoids duplicate/stale rules)
netsh advfirewall firewall delete rule name="%RULE_NAME%" >nul 2>&1

:: Add inbound rule for TCP traffic on the bot's port
netsh advfirewall firewall add rule name="%RULE_NAME%" dir=in action=allow protocol=TCP localport=%PORT%

if %errorlevel% neq 0 (
    echo Something went wrong adding the firewall rule. Check the PORT value and try again.
=======
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
>>>>>>> b8a18a68cdcdd98d618176c2c3192e825c4e1120
    echo.
    pause
    exit /b 1
)

<<<<<<< HEAD
echo Firewall rule added: inbound TCP port %PORT% is now allowed.
echo.
echo ------------------------------------------------------------
echo If you haven't already, you still need to forward this port
echo on your router (external port %PORT% -^> this PC's local IP).
echo ------------------------------------------------------------
echo.

pause

:: --- Detect local LAN IP for same-network access ---
set LOCAL_IP=
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /C:"IPv4 Address"') do (
    set LOCAL_IP=%%a
)
set LOCAL_IP=%LOCAL_IP: =%

:: --- Detect public IP for internet access (requires internet + curl) ---
set PUBLIC_IP=
for /f %%a in ('curl -s ifconfig.me 2^>nul') do set PUBLIC_IP=%%a
=======
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
>>>>>>> b8a18a68cdcdd98d618176c2c3192e825c4e1120

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
<<<<<<< HEAD

pause
endlocal
=======
echo   Save this address - this is what you and
echo   your users will type into their browser.
echo.
echo  =========================================
echo.
pause
>>>>>>> b8a18a68cdcdd98d618176c2c3192e825c4e1120
