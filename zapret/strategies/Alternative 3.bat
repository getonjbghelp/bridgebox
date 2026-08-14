@echo off
chcp 65001 >nul
:: BridgeBox-adapted Flowseal "general (ALT3).bat".
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
  --dpi-desync=fake,hostfakesplit ^
  --dpi-desync-fake-tls-mod=rnd,dupsid,sni=ya.ru ^
  --dpi-desync-hostfakesplit-mod=host=ya.ru,altorder=1 ^
  --dpi-desync-fooling=ts ^
  --dpi-desync-fake-http="%BIN%tls_clienthello_max_ru.bin" ^
  --new ^
  --filter-udp=443 ^
  --hostlist="%HOSTLIST%" ^
  --dpi-desync=fake ^
  --dpi-desync-repeats=6 ^
  --dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"

endlocal
