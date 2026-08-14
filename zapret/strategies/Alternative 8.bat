@echo off
chcp 65001 >nul
:: BridgeBox-adapted Flowseal "general (ALT8).bat".
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
  --dpi-desync=fake ^
  --dpi-desync-fake-tls-mod=none ^
  --dpi-desync-repeats=6 ^
  --dpi-desync-fooling=badseq ^
  --dpi-desync-badseq-increment=2 ^
  --dpi-desync-fake-http="%BIN%tls_clienthello_max_ru.bin" ^
  --new ^
  --filter-udp=443 ^
  --hostlist="%HOSTLIST%" ^
  --dpi-desync=fake ^
  --dpi-desync-repeats=6 ^
  --dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"

endlocal
