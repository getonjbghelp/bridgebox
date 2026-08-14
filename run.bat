@echo off
:: Plain setlocal, no EnableDelayedExpansion: nothing here uses !VAR!, and
:: delayed expansion silently eats "!" out of any path it touches.
setlocal

:: BridgeBox launcher (development / source checkout).
::
:: This file stays pure ASCII on purpose: cmd reads it in the machine's OEM
:: codepage (866 on a Russian Windows), not UTF-8, so anything else comes back
:: as mojibake - harmless in a comment until one mangled byte lands on a quote.
::
:: Self-elevates via UAC: Zapret needs WinDivert, which loads a kernel driver,
:: and installing the local CA into the Trusted Root store needs it too. See
:: zapret/README.md and backend/bridgebox/desktop.py's main().
::
:: Usage:
::   run.bat                 launch normally (no console window)
::   run.bat --console       launch with a visible console, for debugging
::   run.bat --dev           load the UI from the Vite dev server
::   run.bat --rebuild       force a rebuild even if dist looks up to date
::                           (a stale dist rebuilds on its own now - see below)
::   run.bat --minimized     start hidden in the tray (what the autostart task passes)
:: Anything else is forwarded to bridgebox.desktop untouched.
::
:: Startup cost, measured on this machine before trimming it: the elevation
:: relaunch 0.9s, a PowerShell mtime check on the venv 0.9s, a recursive
:: PowerShell mtime scan of frontend/src 2.8s, and a preflight
:: `python -c "import bridgebox.desktop"` 1.3s - roughly seven seconds of shell
:: before the app began to start. Only the UAC relaunch still uses PowerShell,
:: because batch has no way to request elevation without it.

:: "net session" only succeeds in an already-elevated shell, so a non-zero
:: errorlevel here means we have to relaunch ourselves.
:: Two branches below, because -ArgumentList rejects an empty string outright
:: ("the argument is null or empty") - and running run.bat with no arguments is
:: the normal case, so passing '%*' unconditionally made the launcher fail
:: instantly for everyone.
::
:: NOTE for anyone editing the blocks below: "::" comments are illegal inside
:: a parenthesised block. cmd parses the whole block before running any of it,
:: so one "::" line in there breaks the launcher with "- was unexpected at
:: this time" no matter which branch would have been taken. Use REM, or keep
:: the comment out here where it is now.
net session >nul 2>&1
if %errorLevel% neq 0 goto :elevate
goto :elevated

:elevate
echo [BridgeBox] Requesting administrator privileges...
if "%~1"=="" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs -WorkingDirectory '%~dp0'"
) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs -WorkingDirectory '%~dp0'"
)
if errorlevel 1 (
    echo.
    echo [BridgeBox] Could not relaunch as Administrator - see the error above.
    echo [BridgeBox] BridgeBox cannot run without it: Zapret loads a kernel driver.
    pause
)
exit /b

:elevated

cd /d "%~dp0"
echo [BridgeBox] Running as Administrator from %cd%

:: Launcher-only flags are consumed here rather than forwarded: they select
:: which python runs and whether to build, they are not app arguments.
:: Written as labels rather than one if/else block on purpose - inside a
:: parenthesised block "%APP_ARGS%" expands when the block is PARSED, so the
:: variable would never accumulate more than one argument.
set "SHOW_CONSOLE=0"
set "FORCE_BUILD=0"
set "DEV_MODE=0"
set "APP_ARGS="

:parse
if "%~1"=="" goto :parsed
if /I "%~1"=="--console" goto :parse_console
if /I "%~1"=="--rebuild" goto :parse_rebuild
if /I "%~1"=="--dev" set "DEV_MODE=1"
set "APP_ARGS=%APP_ARGS% %~1"
shift
goto :parse

:parse_console
set "SHOW_CONSOLE=1"
shift
goto :parse

:parse_rebuild
set "FORCE_BUILD=1"
shift
goto :parse

:parsed

set "VENV=backend\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "DEPS_STAMP=%VENV%\.bridgebox-deps"

if not exist "%PY%" (
    echo [BridgeBox] No backend virtualenv found - creating one...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [BridgeBox] Failed to create the virtualenv. Is Python 3.11+ installed and on PATH?
        pause
        exit /b 1
    )
)

:: Reinstall when pyproject.toml is newer than the last successful install.
:: Without this, pulling a change that adds or bumps a dependency left the venv
:: silently stale and the app failed at import time with no explanation.
::
:: The comparison used to be a PowerShell one-liner: 0.87s, because that is
:: simply what starting PowerShell costs. The same two os.stat calls through
:: the venv's own interpreter take ~0.08s with -S -E - no site, no environment -
:: and that interpreter has to exist by this point anyway.
set "NEED_DEPS=1"
"%PY%" -S -E -c "import os,sys;s=r'%DEPS_STAMP%';sys.exit(0 if os.path.exists(s) and os.stat(r'backend\pyproject.toml').st_mtime <= os.stat(s).st_mtime else 1)"
if not errorlevel 1 set "NEED_DEPS=0"

if "%NEED_DEPS%"=="1" (
    echo [BridgeBox] Installing/updating backend dependencies...
    pushd backend
    "..\%VENV%\Scripts\python.exe" -m pip install --disable-pip-version-check -q -e ".[dev]"
    if errorlevel 1 (
        echo [BridgeBox] Failed to install backend dependencies.
        popd
        pause
        exit /b 1
    )
    popd
    echo installed> "%DEPS_STAMP%"
)

:: The frontend build.
::
:: --dev never needs this: Vite serves the UI from source and frontend/dist is
:: never read at all, so a stale (or missing) dist is not this script's
:: problem there.
::
:: Otherwise, dist has to be at least as new as everything it was built from,
:: not just "present" - a checkout with an existing dist from before a source
:: edit (frontend/src, or the data files under it: strings/*.json,
:: content/*.json) silently kept serving the old bundle, which is exactly
:: how a link added through build_content.py, or any other source change,
:: went missing from the running app.
::
:: The mtime scan that answers this used to be PowerShell - 2.8 seconds on
:: EVERY launch for a recursive walk of frontend/src, to answer a question
:: that is almost always "no". Same fix as the dependency check above: the
:: venv's own interpreter, no site, no environment. A recursive os.walk over
:: a few hundred files costs single-digit milliseconds there, not because
:: there is less work, but because it is not paying to start a new process
:: (PowerShell itself) to do it.
set "NEED_BUILD=1"
if exist "frontend\dist\index.html" "%PY%" -S -E -c "import os,sys;d=r'frontend\dist\index.html';sys.exit(1 if max((os.stat(os.path.join(p,f)).st_mtime for p,_,fs in os.walk(r'frontend\src') for f in fs), default=0) > os.stat(d).st_mtime else 0)"
if exist "frontend\dist\index.html" if not errorlevel 1 set "NEED_BUILD=0"
if "%FORCE_BUILD%"=="1" set "NEED_BUILD=1"
if "%DEV_MODE%"=="1" set "NEED_BUILD=0"

if "%NEED_BUILD%"=="1" (
    echo [BridgeBox] Building frontend...
    pushd frontend
    if not exist "node_modules" call npm install
    REM Tested by its result rather than by errorlevel: the install above is
    REM conditional, so errorlevel could be left over from anything.
    if not exist "node_modules" (
        echo [BridgeBox] npm install failed. Is Node.js 18+ installed and on PATH?
        popd
        pause
        exit /b 1
    )
    call npm run build
    if errorlevel 1 (
        echo [BridgeBox] Frontend build failed.
        popd
        pause
        exit /b 1
    )
    popd
) else if "%DEV_MODE%"=="1" (
    echo [BridgeBox] Dev mode - the UI comes from the Vite dev server, not from dist.
) else (
    echo [BridgeBox] Frontend dist present - skipping build. Use --rebuild to force one.
)

echo [BridgeBox] Launching...
if "%SHOW_CONSOLE%"=="1" (
    REM Debug mode: keep the console, keep the output, wait for the exit code.
    REM launcher.py leaves a real console alone, so everything still prints here.
    "%PY%" launcher.py %APP_ARGS%
    if errorlevel 1 (
        echo [BridgeBox] BridgeBox exited with an error - see above and logs\bridgebox.log
        pause
    )
    exit /b
)

:: Normal launch. Three decisions, each of which was wrong once:
::
::  - pythonw.exe, not python.exe: python.exe keeps a console window open
::    beside the app for the whole session. That is the black window the
::    "hide console" setting does NOT cover - that setting hides winws's own
::    console, not BridgeBox's.
::  - `start`, because cmd WAITS for pythonw otherwise. Measured: 6.2s against
::    a 6s sleep. Without it this console would stay alive for the whole
::    session, which is the problem pythonw was chosen to solve.
::  - launcher.py rather than -m bridgebox.desktop, because `start` does not
::    pass a "2>file" redirection on to the process it creates - measured, the
::    file comes out empty - so the redirect has to happen inside Python. That
::    is what replaced the 1.3s preflight import: launcher.py points stderr at
::    logs\launcher-stderr.log before importing anything of ours, so a stale
::    venv or a missing dependency lands there instead of nowhere.
::
:: Errors after startup are not lost either: setup_logging() is running by then
:: and everything goes to logs\bridgebox.log. The two hard exits that happen
:: earlier both show a window of their own - the unsupported-Windows notice
:: (platform_support.show_unsupported_notice) and the elevation check, which
:: this script has already satisfied.
start "" "%PYW%" launcher.py %APP_ARGS%

endlocal
