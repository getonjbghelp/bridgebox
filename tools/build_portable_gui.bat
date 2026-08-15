@echo off
:: Launches tools/build_portable.py --gui. Double-click this file, or run it
:: from anywhere - %~dp0 resolves relative to this .bat's own location, not
:: the current directory.

where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0build_portable.py" --gui
    goto :done
)

where py >nul 2>nul
if %errorlevel%==0 (
    py "%~dp0build_portable.py" --gui
    goto :done
)

echo [BridgeBox] Python not found in PATH. Install Python 3, then re-run this file.
pause
exit /b 1

:done
if %errorlevel% neq 0 (
    echo.
    echo [BridgeBox] The builder exited with an error - see above.
    pause
)
