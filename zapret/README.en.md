[![THIS IS AN ENGLISH README. CHECK RUSSIAN VERSION HERE](https://img.shields.io/badge/THIS%20IS%20AN%20ENGLISH%20README-CHECK%20RUSSIAN%20VERSION%20HERE-1d4ed8?style=for-the-badge)](README.md)

# Zapret for BridgeBox

Vendored [Flowseal zapret-discord-youtube](https://github.com/Flowseal/zapret-discord-youtube)
(engine by [bol-van](https://github.com/bol-van/zapret), WinDivert by
basil00). See `../CREDITS.md`.

## Layout

Flowseal's `bin\` is **flattened into this folder** — the `.bat` files here set
`BIN` to the zapret root, not to `bin\`.

```
zapret/
  winws.exe                        DPI-desync engine
  WinDivert.dll  WinDivert64.sys   packet interception (needs UAC)
  cygwin1.dll                      winws.exe is a Cygwin build
  stun.bin  stun2.bin              STUN decoy payloads
  tls_clienthello_max_ru.bin       captured ClientHello, SNI max.ru
  tls_clienthello_4pda_to.bin      captured ClientHello, SNI 4pda.to
  quic_initial_www_google_com.bin  QUIC Initial decoy
  ver.installed.txt                provenance stamp (1.10.0 / Flowseal)
  lists/
    list-jackbox.txt               the only hostlist; every strategy uses it
  strategies/
    General.bat  Alternative 1-12.bat  General (EXP).bat
    Fake TLS Auto*.bat  Simple Fake*.bat        21 adapted files
```

The 21 unmodified Flowseal originals used to sit in `strategies/originalstrategies/`
as a reference for the adaptation. They were removed once the porting was done -
recover them with `git show e7eab7b^:zapret/strategies/originalstrategies/general.bat`
(or any sibling) if a profile ever needs re-checking against its source.

## Config

```yaml
zapret:
  enabled: true
  dir: zapret          # relative to project root
  strategy: general    # slug of a .bat in strategies/ (filename lowercased, non-alnum -> "-")
  hide_console: true   # default; winws's own console window is hidden unless turned off in Settings
```

There is no `binary:` key — `winws.exe` is located from `dir`.

## Strategies

Pick one in Settings (**Strategy**), grouped **Main / Alternatives / Other** by
`group_strategies()`. **Strategy test** switches zapret to each strategy in
turn and times a request to the target set chosen in the picker above it -
**Ecast** (`ecast.jackboxgames.com`, `ecast-prod-use2.jackboxgames.com`),
**Blobcast** (`blobcast.jackboxgames.com`), or **Both**, which is two complete
passes over every strategy back to back, not one pass against the combined
four hosts - a strategy that helps one protocol and hurts the other stays
readable as two separate rows instead of being averaged into one (see
`Api.test_strategies`'s docstring in `backend/bridgebox/desktop.py`). Results
can be exported to JSON or HTML from the same popup. The suite skips the
`Other` group by default - Fake TLS Auto / Simple Fake are slow to probe.

Some strategies failing the test is **normal**: a profile that doesn't pass
Jackbox upstream is a result, not an error.

Domains come **only** from `lists/list-jackbox.txt` via `--hostlist=`. There is
no inline `--hostlist-domains` in any adapted .bat — edit the list, not the bat.

Editable from Settings (**Network and bypass** → **Bypass domains**), which
validates at the boundary and writes atomically: winws does not reject a
malformed line, it silently ignores it, so a bad entry would otherwise surface
as "zapret bypassed nothing" at the next bridge start, far from the edit that
caused it. The list currently covers 12 official Jackbox hosts only - Ecast
(4), Blobcast (3), and shared/CDN hosts (5). Unofficial mirrors (`jackbox.fun`
and similar) are **not** included by default; add them by hand here if needed,
the same way - see the root `README.en.md`'s troubleshooting section for why
they were dropped.

### Port 38203 - Blobcast's socket.io session

Every one of the 21 strategies carries `38203` in **both** `--wf-tcp` and
`--filter-tcp`, alongside 80/443. That is not the bridge's port - it is the
port the Blobcast-speaking game itself opens its long-lived socket.io session
on, found by packet capture (see `server/blobcast.py`'s module docstring). It
has to be in both filters or that session gets no DPI bypass at all: measured,
the first connection through it succeeds and every repeat times out, which is
what an unstable long Blobcast session looked like before this was added.

If the port is ever changed for a non-official Blobcast server (Settings →
**Connection profiles** → the Blobcast profile → **socket.io port**), every
strategy needs the new port added the same way, or that profile loses its
bypass silently. `test_blobcast_port_is_in_the_dpi_filters` pins the port
number to `server/blobcast.py`'s `SOCKETIO_PORT` so a mismatch fails loudly
instead.

### Legacy names

Old config keys that still resolve:

- `general-alt11` → `Alternative 11.bat`

An alias whose target isn't in `strategies/` is worse than no alias — it turns
"unknown strategy" into an error naming a file that was never shipped.
`test_strategy_assets.py` enforces that every alias resolves.

## What the adaptation changed, and what it dropped

Each of the 21 adapted files maps 1:1 to a Flowseal original in
`strategies/originalstrategies/` (named in each file's header). Three things
had to change, all forced by the layout:

1. **No `service.bat`.** The originals `call service.bat` four times to fetch
   updates and populate `%GameFilterTCP%` / `%GameFilterUDP%`. Without it those
   variables expand to empty and produce malformed `--wf-tcp=…,`.
2. **No `start /min`.** The originals launch winws detached; BridgeBox tracks
   the PID, and a detached launch would hand it the batch launcher's PID
   instead. Every adapted file runs winws in the **foreground**.
3. **Only 2 of 9 profiles survive.** The originals carry nine `--new` sections:
   Discord voice UDP, `discord.media`, Google, `ipset-all` catch-alls, and
   `%GameFilter*%` game ranges — **none of which apply to Jackbox**. The two
   that do (TCP 80,443 and QUIC/UDP 443) are ported. The dropped sections also
   needed 12 files that were never vendored here, chiefly
   `tls_clienthello_www_google_com.bin`, `list-general.txt`, `ipset-all.txt`
   and the `list-exclude*` / `ipset-exclude*` pairs.

Consequently every `--hostlist-exclude`, `--ipset`, `--ipset-exclude`,
`--filter-l7`, `--ip-id`, `--dpi-desync-cutoff` and `--dpi-desync-any-protocol`
flag is gone. That is deliberate: with a hostlist this small (12 official
Jackbox domains, no mirrors) there is nothing to exclude.

Restoring the *dropped* profiles (Discord voice, `discord.media`, Google,
the `ipset-all` catch-alls, `%GameFilter*%`) is not possible from this repo —
the originals were only ever `.bat` files, and none of the assets they
reference (the `.bin` payloads, `list-general.txt`, the `ipset-*` files) were
vendored. That is a different question from adapting a strategy this repo has
never seen before - see "Adapting a new strategy" below.

## Do not drop a raw Flowseal root here unchanged

It will not work, for reasons 1–3 above. Port the winws invocation into the
existing preamble instead (guards for `winws.exe` and the hostlist, `BIN`/`LISTS`
pointing at this folder, foreground launch).

`test_strategy_assets.py` enforces both directions: every `%BIN%` file a
strategy names exists, and every `.bin` on disk is used by some strategy. The
second one is the guard that matters — three strategies had silently
substituted `tls_clienthello_max_ru.bin` for the pattern their original used,
leaving `stun2.bin` and `tls_clienthello_4pda_to.bin` dead on disk.

## Adapting a new strategy

`backend/bridgebox/zapret/strategy_adapt.py` mechanises what porting the
original 21 did by hand: given a raw Flowseal `general*.bat` this repo has
never seen, pick out the two Jackbox-relevant profiles and render them into
BridgeBox's own template - the same selection this file's "What the
adaptation changed" section above describes, done programmatically instead
of by reading each file.

It is a **token whitelist generator, not a text transformer.** A `.bat`
assembled from downloaded text and later run as Administrator is a
command-injection sink, so the original text is never copied or executed:
it is parsed into `--flag=value` tokens, and only `dpi-desync*` flags whose
value matches a strict charset (or the `%BIN%<safe-name>.bin` form) survive
into the render. Selection rule, measured against all 21 shipped
adaptations rather than assumed: the TCP profile whose `--filter-tcp` covers
both 80 and 443, and the QUIC profile carrying `--dpi-desync-fake-quic`,
each preferring `--hostlist` over `--ipset`. A strategy missing either
profile, or naming a `.bin` payload that was not shipped alongside it, is
skipped with a reason rather than written half-working.

The golden test (`backend/tests/test_strategy_adapt.py`, fixtures in
`backend/tests/fixtures/flowseal-1.10/originalstrategies/`, recoverable via
`git show e7eab7b^:zapret/strategies/originalstrategies/<name>`) asserts the
adapter reproduces every one of the 21 shipped files' flags exactly. This
never overwrites an existing hand-adapted strategy - only a qualifier not
already present in `strategies/` gets a new file.

## Updating the vendored binaries

`backend/bridgebox/zapret/update.py` checks Flowseal's GitHub releases and
can replace `winws.exe`, `WinDivert*`, `cygwin1.dll` and the `.bin` payloads
- from Settings → **Zapret update**. Pinned to `Flowseal/zapret-discord-youtube`
and to `github.com`/`objects.githubusercontent.com` after redirects; the
downloaded archive is size- and member-capped, and `winws.exe` is checked for
a PE header (`MZ`) before anything is applied, since it is a code-execution
source that later runs elevated.

Only payloads some adapted strategy actually references get installed
(`select_for_install`) - a release ships payloads for profiles BridgeBox
never ported (Discord, Steam, Tencent, game-port ranges), and copying those
in unreferenced is exactly the state `test_every_payload_file_is_used_by_some_strategy`
exists to forbid. **Strategy `.bat` files are never touched by this path** -
see "Do not drop a raw Flowseal root here unchanged" above for why.

## Notes

- With `zapret.enabled: true`, BridgeBox requests **UAC** at launch (WinDivert
  needs to load a kernel driver).
- Stop kills **only the process tree BridgeBox itself started**, and only if
  this session started it: `taskkill /F /T /PID <pid>`. The `/T` is not
  optional - the tracked PID is the strategy `.bat`'s `cmd.exe` host, and
  `winws.exe` is its child, so killing the PID alone leaves winws running with
  its WinDivert handle open. A winws the user launched by hand, or one left
  over from a previous BridgeBox session, is deliberately untouched.
- The machine-wide `taskkill /F /IM winws.exe` still exists as
  `process.kill_all_winws()`, with exactly one justified caller: the updater,
  which is about to overwrite `winws.exe` and `WinDivert64.sys` and cannot
  proceed while any process holds them.
