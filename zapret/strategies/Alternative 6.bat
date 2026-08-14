@echo off
chcp 65001 >nul
:: BridgeBox-adapted Flowseal "general (ALT6).bat".
:: Layout: winws.exe + WinDivert + *.bin live in vendor/zapret/ (parent of this file).
:: Runs winws in the foreground (no "start") so BridgeBox can track the process.
:: Domains come only from lists\list-jackbox.txt (--hostlist).

setlocal
cd /d "%~dp0.."
set "BIN=%cd%\"
set "LISTS=%cd%\lists\"
set "HOSTLIST=%LISTS%list-jackbox.txt"

if exist "%BIN%winws.exe" goto :bb_check_hostlist
echo [BridgeBox] winws.exe not found in "%BIN%"
exit /b 1

:bb_check_hostlist
if exist "%HOSTLIST%" goto :bb_run
echo [BridgeBox] hostlist not found: "%HOSTLIST%"
exit /b 1

:bb_run

"%BIN%winws.exe" ^
  --wf-tcp=80,443,38203 ^
  --wf-udp=443 ^
  --filter-tcp=80,443,38203 ^
  --hostlist="%HOSTLIST%" ^
  --dpi-desync=multisplit ^
  --dpi-desync-split-seqovl=681 ^
  --dpi-desync-split-pos=1 ^
  --dpi-desync-split-seqovl-pattern="%BIN%tls_clienthello_www_google_com.bin" ^
  --new ^
  --filter-udp=443 ^
  --hostlist="%HOSTLIST%" ^
  --dpi-desync=fake ^
  --dpi-desync-repeats=6 ^
  --dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"

endlocal
