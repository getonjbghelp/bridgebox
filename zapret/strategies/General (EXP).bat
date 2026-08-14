@echo off
chcp 65001 >nul
:: BridgeBox-adapted Flowseal "general (EXP).bat".
:: The original raw Flowseal file depended on service.bat, bin\, lists\list-
:: general*.txt/ipset-*.txt and several .bin files (ACTIVE_DISCORD_UDP.bin,
:: quic_initial_4pda.to.bin, tls_clienthello_www_google_com.bin,
:: ACTIVE_GAME_UDP.bin) that don't exist in this layout - it also used
:: `start /min`, which would have broken ZapretProcess's PID tracking (the
:: tracked PID would belong to the batch launcher, not winws.exe).
::
:: Ported here: the original's TCP 80,443 profile (its line 21) and its QUIC
:: profile (line 17), which are the only two of its nine that apply to
:: Jackbox traffic. The rest target Discord voice, discord.media, Google, and
:: %GameFilter*% port ranges.
::
:: An earlier adaptation mistakenly copied the original's *discord.media*
:: profile instead (ports 2053/2083/2087/2096/8443 - seqovl=681, repeats=8),
:: which is why this file used to carry seqovl=680/repeats=8 on 80,443. The
:: values below are the original's real 80,443 numbers. Versus General.bat
:: this is still the heavier fallback: it adds a fake packet and fooling=ts
:: on top of the same multisplit.

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
  --dpi-desync=fake,multisplit ^
  --dpi-desync-repeats=4 ^
  --dpi-desync-split-seqovl=480 ^
  --dpi-desync-split-pos=1 ^
  --dpi-desync-fooling=ts ^
  --dpi-desync-split-seqovl-pattern="%BIN%stun2.bin" ^
  --dpi-desync-fake-tls="%BIN%tls_clienthello_max_ru.bin" ^
  --dpi-desync-fake-http="%BIN%tls_clienthello_max_ru.bin" ^
  --new ^
  --filter-udp=443 ^
  --hostlist="%HOSTLIST%" ^
  --dpi-desync=fake ^
  --dpi-desync-repeats=11 ^
  --dpi-desync-fake-quic="%BIN%quic_initial_www_google_com.bin"

endlocal
