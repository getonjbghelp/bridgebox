<div align="center">

[![THIS IS AN ENGLISH README. CHECK RUSSIAN VERSION HERE](https://img.shields.io/badge/THIS%20IS%20AN%20ENGLISH%20README-CHECK%20RUSSIAN%20VERSION%20HERE-1d4ed8?style=for-the-badge)](README.md)

<img src=".github/readme/logo.svg" alt="BridgeBox" width="360" />

*Made with Claude Sonnet 5 / Opus 5*

[![Release](https://img.shields.io/github/v/release/getonjbghelp/bridgebox?label=release&color=1d4ed8)](https://github.com/getonjbghelp/bridgebox/releases/latest)
[![License](https://img.shields.io/badge/license-PolyForm%20NonCommercial%201.0.0-1d4ed8)](LICENSE.md)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-1d4ed8)](#download)
[![Downloads](https://img.shields.io/github/downloads/getonjbghelp/bridgebox/total?label=downloads&color=1d4ed8)](https://github.com/getonjbghelp/bridgebox/releases)
[![Telegram](https://img.shields.io/badge/Telegram-%40bridgeboxofficial-1d4ed8?logo=telegram&logoColor=white)](https://t.me/bridgeboxofficial)

</div>

BridgeBox opens a local bridge between Jackbox Party Pack and your ISP and routes the
game's traffic around DPI blocking. It runs entirely on your own computer, touches
nothing on the system beyond one local certificate, and needs neither a VPN nor a
rented server.

---

> [!CAUTION]
> ### Fakes
> The project's official Telegram channel is **[@bridgeboxofficial](https://t.me/bridgeboxofficial)**,
> and there is no other. The only official place the program is distributed from is the
> **[Releases](https://github.com/getonjbghelp/bridgebox/releases/latest)** tab of this
> repository. Anything else calling itself BridgeBox isn't us.

> [!WARNING]
> ### Antivirus software and WinDivert
> DPI bypass runs on WinDivert - a driver for intercepting and filtering traffic, the
> same one the original [zapret](https://github.com/bol-van/zapret) and dozens of other
> projects use. A legitimate tool, but some antivirus software (Windows Defender
> included) occasionally flags it as a potentially unwanted application and quarantines
> it - the detection is usually named something like `WinDivert` or
> `Not-a-virus:RiskTool.Multi.WinDivert`.
>
> If that happens, add the BridgeBox folder to your antivirus's exclusions, or turn off
> PUA detection. Without the WinDivert files the bypass simply won't start, and the
> program will say so plainly in the logs rather than just quietly failing.

---

## Table of contents

- [What it is and why](#what-it-is-and-why)
- [Download](#download)
- [Connecting the game](#connecting-the-game)
- [Screens](#screens)
- [All settings](#all-settings)
- [Troubleshooting](#troubleshooting)
- [How it works under the hood](#how-it-works-under-the-hood)
- [What BridgeBox can't do yet](#what-bridgebox-cant-do-yet)
- [For developers](#for-developers)
- [Where everything lives](#where-everything-lives)
- [Security and privacy](#security-and-privacy)
- [Support the project](#support-the-project)
- [License](#license)

---

## What it is and why

Jackbox games talk to their servers over two protocols, `Ecast` and `Blobcast`. In some
countries that connection is unreliable: rooms create only some of the time, players
join with a huge delay, or the game never starts at all. The cause is DPI - deep packet
inspection on the ISP's side. Unofficial relay servers solve the same problem, but they
run on whatever VPS happened to be available and go down along with it.

BridgeBox solves it with two parts working together:

1. **Zapret** - bypasses DPI at the network packet level. This is a ready-made engine
   (`winws.exe` by bol-van, in Flowseal's build) that BridgeBox drives directly: starts
   it, stops it, switches its strategy, benchmarks strategies against each other, and
   can update its files from GitHub on its own.
2. **The bridge** - a local HTTPS server at `127.0.0.1:PORT`. You point the game at it
   as its server address, and all of its traffic passes through - one place where
   everything is visible in the logs and everything is under your control.

Jackbox games speak **two different protocols** depending on the title: Party Pack 7
and newer use Ecast, older ones (Party Pack 1-6 and a handful of standalone titles) use
Blobcast. The bridge serves **both at once** - not a mode switch, each protocol simply
has its own destination address, configured separately (see
[Connection profiles](#all-settings)).

The key difference from unofficial relay servers: BridgeBox **has no idea what game
you're playing**. It forwards the protocol whole, not a fixed set of pre-written
requests, so a new Party Pack never needs a program update to work.

---

## Download

The current version always lives in the **[Releases](https://github.com/getonjbghelp/bridgebox/releases/latest)**
tab of this repository, not in the Code tab (that's the source for development - see
[For developers](#for-developers)).

1. Download `BridgeBox_Portable.zip` from the latest release and unpack it anywhere -
   a USB stick works fine. Nothing gets installed on the system: removing the program
   means deleting the folder, nothing more.
2. Run `bridgebox.exe`. Windows will ask for administrator rights - without them
   BridgeBox can neither load the WinDivert driver nor install the local certificate;
   decline and the program will say so plainly and close.
3. The first launch walks you through a setup wizard: interface language, then picking
   a bypass strategy that actually works with your ISP.

Every copy you download runs independently: its own certificates, its own settings, its
own logs. Nothing is written to the registry or `%APPDATA%`, so you can keep several
versions side by side or move a working copy to another computer just by copying the
folder.

---

## Connecting the game

The **"How do I connect the game?"** button on the home screen opens a step-by-step
guide with your current address and port filled in. It starts by asking which platform
you're on - **Steam** or **other copies** (not through Steam) - since they need the
server address set a different way; everything after that is the same.

### Steam

The **"Insert Automatically"** button at the top of the guide finds your installed
Steam copies of Jackbox itself and sets the launch option for you - nothing to search
for or edit by hand. Applying it closes and reopens Steam (any game currently running
through it will stop); you can undo it from the same place with the "Restore previous
setting" button.

If you'd rather do it by hand (or Steam wasn't found automatically):

1. In Steam, right-click the Party Pack game → **Properties**.
2. **General** tab → **Launch Options** field.
3. Paste this line in:

   ```
   -jbg.config serverUrl=127.0.0.1:PORT
   ```

   **No `https://` at the start.** The game supplies the scheme itself and ignores an
   address that already has one - confirmed in practice.

   If you changed the port in Settings, use that port here instead.

4. Close Properties and launch the game.

### Other copies (not through Steam)

The same **"Insert Automatically"** button here scans every local drive: it finds
Jackbox shortcuts and `jbg.config.jet` files and lets you pick which ones to fix. It
only patches what already exists - nothing gets created from scratch, so a copy with
no shortcut and no config file simply won't show up in the list. A full drive scan can
take a couple of minutes; a folder-checked counter shows it's still working.

If you'd rather do it by hand - two ways, the in-app guide has a short animation for
each step.

**Method 1 - via a shortcut (simpler).** Create a shortcut to the game's `.exe`, open
its Properties → **Target** field, and after the closing quote add a space and the same
`-jbg.config serverUrl=127.0.0.1:PORT` line. Launch the game through that shortcut from
then on.

**Method 2 - edit the game's own config (more flexible).** Inside the copy of the game,
in the `games` folder, find the right Party Pack and in its `jbg.config.jet` replace the
`serverUrl` value with `127.0.0.1:PORT` (no scheme). This way you can choose exactly
which installed game goes through the bridge without touching the others.

### After connecting

The game creates a room through the bridge as usual, and the room code shows up on
screen the normal way. Players go to **jackbox.fun / jackbox.ru / jackbox.tv / others**
and enter the code - nothing different from a normal game.

### Checking that it works

Once the bridge is on, the home screen shows a **"Test connection (ping)"** button. It:

- pings the real Jackbox servers - both Ecast and Blobcast;
- creates an actual test room through the bridge;
- confirms the room registered;
- deletes it afterwards.

Each step prints its own line, so if something's wrong you can see exactly where.

---

## Screens

The panel on the left collapses into a narrow icon strip (the logo folds into a "bb"
monogram) - the button at the bottom toggles it, and the state is remembered between
launches. On first run, the setup wizard walks you through language and bypass strategy
before the regular window ever opens.

On first run (or for as long as the version is still tagged beta), a **β** mark sits
next to the logo - hovering it explains what that means, and clicking it opens the
version history.

### Home

The main screen: a big toggle, bridge status, the address with a copy button, the
connection test button, and the connection guide.

### Settings

Four sections, covered [below](#all-settings).

### Info

The logo, a short description of the program, the current version, file integrity
status, the program's license, a button listing every third-party component (name,
author, license, and a link for each - the same list as [CREDITS.md](CREDITS.md)), and
link buttons (social, donations, and so on) - each either opens straight to its address
or shows a popup with text. The megaphone button in the sidebar
leads to the same place the Info screen does - GitHub Issues or a feedback form, for
reporting a bug. The Info screen's content and the version history behind the β mark
above aren't hardcoded text, they're data under `frontend/src/data/content/`, which a
developer edits through `tools/build_content.py` without touching the source.

### Logs

A live feed of what the program is doing, refreshed roughly once a second while the
screen is open.

- **Level filter** - four buttons: DEBUG, INFO, WARNING, ERROR. Click to toggle which
  lines show.
- **Search** - matches the message text, module name, and function name.
- **Copy** - puts what's currently visible (after filters) on the clipboard.
- **Clear** - clears the on-screen list (it's already written to the file, so nothing is
  lost).
- The feed auto-scrolls to new entries. Scroll up to read something and auto-scroll
  pauses, with a **"↓ New entries"** button to jump back down.

Logs are also written to `logs/bridgebox.log`, rotating at 5 MB.

---

## All settings

### Language and appearance

| Setting | What it does |
|---|---|
| **Language** | "Same as system" (detected automatically), Russian, or English. Applies instantly, no restart - across every screen, the tray, and the browser stub pages. |
| **Dark theme** | Switches between light and dark. Persists. The window title bar (the strip Windows itself draws, with the close button) recolours along with the interface - fully on Windows 11, only its lightness on Windows 10, since the colour itself stays system-controlled there for reasons outside the app's control. |
| **Interface animations** | Turns off every transition and animation. Useful on slower machines. |

### Startup and tray

| Setting | What it does |
|---|---|
| **Start with Windows** | Creates a Task Scheduler task with the highest privilege level and a raised priority (not a registry Run key - without administrator rights the bypass couldn't work anyway, and the priority means BridgeBox doesn't wait in line behind other startup programs). Right next to it, **"Start minimized to tray"** keeps the window from appearing at all on login. |
| **Turn on the bridge automatically** | The bridge comes up by itself when the program opens, no need to touch the toggle. |
| **Minimize to tray on close** | The close button hides the window instead of killing the bypass mid-game. Full exit is through the tray icon's own menu. The tray icon is live: its tooltip and its "Stop the bridge" item always reflect the bridge's actual state. |

### System

| Setting | What it does |
|---|---|
| **Hide the console** | On by default - the bypass runs in the background with no separate black window next to the app (this also applies during the wizard's strategy auto-test). Turn it off to watch Zapret's own output live. |
| **Bridge port** | The local bridge's port, 8443 by default. Change it if that port is taken. Takes effect on the bridge's next start. **Reset** brings it back to 8443. |
| **Temp file folder** | Where a Zapret update gets downloaded and unpacked (see below). |

### Network and bypass

| Setting | What it does |
|---|---|
| **Strategy** | The DPI bypass method. 21 presets ship by default, grouped **Main / Alternatives / Other**. Different ISPs need different strategies - that's normal, not a bug. |
| **Strategy test** | Cycles through every strategy in turn. You can choose what to ping against - Ecast, Blobcast, or both (two full passes, one after the other). Afterwards it offers to apply the fastest one and to save the results as JSON or HTML. Takes a few minutes; "Both" takes a bit longer. |
| **Test everything** | Adds the "Other" group (Fake TLS Auto, Simple Fake) to the run. There are a lot of them and they're slow, so they're skipped by default. |
| **Bypass domains** | The list of sites the bypass applies to. One name per line; lines starting with `#` are comments. Takes effect on the bridge's next start. |

**About the strategy test:** some strategies failing is the expected outcome, not an
error. You only need one that works.

### Zapret update

Checks for and pulls the latest `winws.exe`, WinDivert, and `.bin` bypass files straight
from Flowseal's GitHub release. It also adapts that release's strategies to BridgeBox's
own format: new ones get added, ones it already knows get updated in place, and any you
edited by hand are never overwritten - a file with a "(updated)" suffix appears next to
them instead. Details in [`zapret/README.en.md`](zapret/README.en.md).

| Setting | What it does |
|---|---|
| **Installed version** | Read from `zapret/ver.installed.txt`. |
| **Check on startup** | Off by default - GitHub is unreachable from some countries. When on, the check runs in the background right after launch without delaying the window opening; the result shows up right here, the same as if you'd clicked "Check for updates" yourself. |
| **Check for updates** | A one-off request to GitHub. If a newer version exists, an "Update" button appears. |
| **Update** | Downloads the release archive, unpacks it to a temp folder, and replaces only the files actually in use. Asks for confirmation first; if the bridge is running, the bypass pauses for the duration of the swap. |

After an update the program offers to restart - the new files only take effect once it
does.

This only updates the bypass engine. Updating BridgeBox itself is the next section.

### Updating BridgeBox

Separate from Zapret - the program checks [its own releases on
GitHub](https://github.com/getonjbghelp/bridgebox/releases/latest), shows what changed,
and, in a built version (not running from source), can update itself without a trip to
the browser: it downloads the `.exe`, verifies its checksum against the one GitHub
itself computed when the release was published, and swaps the running file in. A
critical security update shows a dedicated red banner that doesn't go away for good
until it's installed.

| Setting | What it does |
|---|---|
| **Installed version** | BridgeBox's own version (not Zapret's). |
| **Check on startup** | **On** by default - unlike Zapret's check, this is the same channel critical security warnings arrive through. |
| **Check for updates** | A one-off request to GitHub. |
| **Update BridgeBox** | Downloads, verifies the checksum, and swaps the `.exe` in. On success it offers to restart - the new version only takes effect after that. Unavailable when running from source - update with `git pull` instead. |

### Connection profiles

A profile is a server address plus the settings specific to its protocol. **Ecast and
Blobcast are always active at the same time** - not a switch: each protocol has its own
active profile, and both apply to their own slice of traffic without any input from you.
A profile's "protocol type" decides which requests reach it and which settings apply to
it.

The two official profiles (**Official Ecast**, **Official Blobcast**) can't be deleted,
renamed, or have their address or type changed - a guarantee that each protocol always
has somewhere to send traffic, even if every profile you created yourself gets removed.

**Editing a profile and using it are two separate actions.** There can be several
profiles of the same type (a mirror next to the official server, say), but only one is
ever active. The dropdown picks which profile you're currently editing; the **"Make
active"** button decides which one the bridge actually uses.

| Setting | What it does |
|---|---|
| **Profile** / **+** | Pick the profile you're editing; **+** creates a new one (a copy of the official Ecast profile by default). |
| **Protocol type** | Ecast or Blobcast. Locked for the official profiles. |
| **Name**, **Server address** | `https://` only. Locked for the official profiles. |
| **Active profile** / **Delete** | Make it the one in use, or remove it (except the official ones). Takes effect on the bridge's next start. |
| **Move profiles** | Export/import your own profiles as a file or as text. Import only adds - it never touches existing profiles or changes which one is active. Official profiles never travel with this. |

**Ecast profile settings** (response rewriting and how much gets proxied):

| Setting | What it does |
|---|---|
| **Proxy all traffic** | **On by default.** Every path that isn't Blobcast's goes to this profile's address. Turn it off and only the paths listed below are forwarded. |
| **Ecast paths** | Comma-separated, each starting with `/`. `/api, /tts, /media` by default. Only matters when "proxy all traffic" is off - has no effect on Blobcast, which has its own list. |
| **Keys to rewrite** | Which JSON fields carrying the server address get swapped for the bridge's own. Off by default - the response reaches the game unmodified. |
| **Room code keys** | Where to look for the room code. Doesn't change what the game receives - purely for diagnostics, so there's no toggle for it. |
| **Origin** | Whether to rewrite the Origin header. Off means the game's own value goes through untouched. |
| **User-Agent** | Substitutes a browser-like User-Agent when the game didn't send one. Without it the API answers with a 403. |

The three rewriting toggles are **off by default** - a direct connection already works
thanks to Zapret, and rewriting only matters for experiments or a non-standard server.

**Blobcast profile settings** (the Party Pack 1-6 session runs on its own protocol over
socket.io, and the bridge intercepts it on a separate port):

| Setting | What it does |
|---|---|
| **Intercept the game session** | On by default. Turn it off and the session goes straight to Jackbox, bypassing the bridge - rooms still get created, the bridge simply won't see the session. A fallback for if interception ever breaks. |
| **Hostname for the game** | A bare hostname (no `https://`, no port) - the game fills in the port itself. The default, `localhost`, shouldn't need changing without a real reason. |
| **Log session frames** | Off by default - otherwise the log fills up fast with session debug data. |
| **Blobcast paths** | Which requests count as Blobcast rather than Ecast. |
| **socket.io port** | The port the game opens its session on, 38203 by default. This exact port has to be in Zapret's strategy filters too, or DPI bypass on it simply doesn't happen. |

The reset and delete buttons each have their own **"Don't ask again"** checkbox -
turning off one doesn't turn off the others.

### Reset settings

Its own section at the very bottom of the Settings screen - the one action here that
touches every other section at once, rather than a single one.

| Setting | What it does |
|---|---|
| **Reset all settings** | Puts the whole app back to factory defaults - network, strategy, connection profiles, theme. Asks for confirmation first. The bypass domain list is **not** touched by this. |

---

## Troubleshooting

### The game doesn't create a room

1. Make sure the bridge is **on** - the home screen should show "Bridge is running".
2. Click **"Test connection (ping)"** and see which step fails:
   - **ping fails** → a DPI bypass problem, head to the strategy test;
   - **room creation: network error** → the bridge couldn't reach Jackbox;
   - **everything's green but the game still doesn't work** → check that the connection
     parameter actually reached the game (see
     [Connecting the game](#connecting-the-game)).
3. Double-check the parameter itself: `-jbg.config serverUrl=127.0.0.1:8443`, **no
   `https://`**, port matching what the program shows.

### It worked, then stopped

ISPs change their DPI rules from time to time. Open **Settings → Strategy test**, let it
finish, and apply the fastest result. This fixes it more often than anything else.

### Zapret won't start

The log will show `zapret failed to start`. Check:

- the program is running **as Administrator**;
- your antivirus hasn't deleted `zapret\winws.exe` or `WinDivert64.sys` (see
  [the warning above](#antivirus-software-and-windivert));
- there isn't a second copy of zapret running by hand or left over from an earlier
  BridgeBox session - stopping the bridge only kills the process **this** session
  started; a `winws.exe` it didn't start is left alone on purpose.

### Port is in use

Change the port under **Settings → System and appearance → Bridge port**, and remember
to update the connection parameter the same way you set it originally (see
[Connecting the game](#connecting-the-game)).

### Third-party mirror sites don't work

Mirrors (`jackbox.fun` and similar) are third-party services with nothing to do with
BridgeBox, and **their domains aren't in the bypass list by default** - only Jackbox's
own real hosts are. The problem is likely on the mirror's own end: it's down, it can't
reach your room's server, or the official servers are timing it out - and that only gets
fixed there, not in BridgeBox. Play through **jackbox.tv** instead: with a working bypass
it's reachable directly and needs nothing extra.

### Nothing helped

Open **Logs**, turn on every level, reproduce the problem, and click **"Copy"**. Tokens
and passwords are redacted automatically, so the result is safe to attach to a bug
report. From there - through the megaphone in the sidebar or the Info screen - file an
issue on [GitHub](https://github.com/getonjbghelp/bridgebox/issues) or write in through
the feedback form, logs attached.

---

## How it works under the hood

```
Game (Steam or another copy)
    │  serverUrl=127.0.0.1:PORT
    ▼
BridgeBox - a local HTTPS server
    │  Ecast (/api/v2/*) and Blobcast (/room, /accessToken, /socket.io)
    │  are told apart by path and sent to their own profile
    ▼
Zapret / WinDivert - bypasses DPI at the packet level
    │
    ▼
Jackbox's real servers
```

What the bridge does with every HTTP request:

1. Accepts the request from the game over HTTPS (with its own local TLS certificate -
   the game does talk to it over HTTPS).
2. Works out from the path whether it's Ecast or Blobcast, and takes the address from
   the matching active profile.
3. Rewrites the `Host` header to the real server's address, or that server wouldn't know
   which site is being asked for.
4. Forwards the request **whole**: path, query, headers, and body, unchanged and
   untruncated.
5. Gets the response back and hands it to the game, stripping only hop-by-hop transfer
   headers.

**The bridge doesn't cap request or response size.** Log lines get truncated at 800
characters with a `... (+N bytes)` note, but that's the log entry only - the actual data
passes through in full.

The Blobcast session (Party Pack 1-6) is a separate thing: it doesn't run over HTTP, it
uses its own protocol on top of socket.io, on a port **the game itself picks** (38203 by
default). The bridge opens a dedicated listener for it and relays session frames
verbatim in both directions - that's exactly what the "Intercept the game session"
setting in the Blobcast profile turns off.

Separate pieces worth knowing about:

- **The local certificate.** On first launch BridgeBox creates its own certificate
  authority and a certificate for `localhost`, installed into the trusted root store.
  Without it the game couldn't connect to the bridge over HTTPS at all - and Blobcast
  session interception depends on the same certificate. It never leaves your machine: it
  only signs `localhost`, and the private key is restricted to the current user and
  administrators (more in [Security](#security-and-privacy)).
- **The browser stub page.** Open `https://127.0.0.1:PORT` in a browser and you'll get a
  warning page. The game never sees this - it's only served to whatever asks for HTML.
- **The Ecast WebSocket relay.** It exists in the code but isn't used while response
  rewriting is off: the game connects to the real server directly. Not to be confused
  with the Blobcast session interception above - the two are independent mechanisms.

---

## What BridgeBox can't do yet

- **Isn't code-signed.** On first launch, Windows SmartScreen may show "Windows
  protected your PC". That's the standard reaction to any new exe without a code-signing
  certificate, not a sign that something's wrong - click "More info" → "Run anyway".
- **Windows 10/11 only.** WinDivert is a Windows-only driver, and the interface runs on
  WebView2, which doesn't exist on other systems or on Windows 7/8/8.1.

---

## For developers

Building and changing the code doesn't need Releases at all - everything required is in
this repository.

### What you need

| Requirement | Why |
|---|---|
| **Windows 10/11 (64-bit)** | WinDivert is a Windows-only driver; the interface is WebView2, which Microsoft no longer ships for Windows 7/8/8.1 |
| **Administrator rights** | Zapret loads a driver into the kernel; the TLS certificate goes into the system store |
| **Python 3.11+** | The backend |
| **Node.js 18+** | Building the interface (only needed to build it, not for every launch) |

### Running from source

1. Double-click **`run.bat`**.
2. A UAC prompt appears - accept it. The program won't start without administrator
   rights (and will say so plainly before closing).
3. On the **first** run, the script creates a Python virtual environment, installs
   dependencies, and builds the interface. Takes a couple of minutes.
4. After that, launches are fast: dependencies only reinstall when
   `backend/pyproject.toml` changed, and the interface only rebuilds when files under
   `frontend/src` changed since the last build (including the interface text and the
   Info screen's content - edits made through `tools/edit_ui_strings.py` and
   `tools/build_content.py` are picked up on the next launch automatically). The
   `--rebuild` flag forces a rebuild regardless.
5. The BridgeBox window opens, and everything from here on is the same as the portable
   version.

Other `run.bat` flags (source checkout only, not in the portable build): `--console`
shows a console window with output (for debugging), `--dev` serves the interface from
the Vite dev server instead of a build, `--minimized` starts straight into the tray
(used by the autostart task).

### Building your own portable release

```
python tools/build_portable.py
```

Builds `dist/BridgeBox_Portable/` the same way the files on the Releases tab are built:
a clean `frontend/dist`, `bridgebox.exe` through PyInstaller (no console window,
administrator rights requested through an embedded manifest, the version baked in
straight from `backend/pyproject.toml`), and alongside it an up-to-date `zapret/`,
empty `certs/`/`temp/`/`logs/`, and a clean `config.yaml`. At the end the script checks
its own output: that every required file is present, that no path from the build
machine leaked into the exe, and that the integrity manifest (the Info screen's file
integrity status) actually matches what's really in the folder.

The `--icon path\to\file.ico` flag swaps in your own icon instead of the generated one.
`--skip-frontend-build` skips rebuilding the interface if `frontend/dist` is already
current.

---

## Where everything lives

```
BridgeBoxDevVersion/
  run.bat                        launches from source (asks for admin rights itself)
  config.yaml                    every setting, editable by hand
  CREDITS.md                     third-party projects and their licenses
  LICENSE.md                     BridgeBox's own license
  certs/                         local certificate (created automatically)
  logs/bridgebox.log             activity log, rotates at 5 MB
  backend/                       the Python server side
    bridgebox/                   all backend code
    tests/                       automated checks
  frontend/                      the React interface
    src/screens/                 screens: Home, Settings, Logs, Info, the wizard
    src/components/              reusable buttons, toggles, and so on
    src/data/strings/ru.json,    interface text in both languages
              en.json
    src/data/content/            version history and the Info screen's content
      changelog.json, about.json
  zapret/                        the DPI bypass engine
    strategies/                  strategies (adapted for BridgeBox)
    lists/list-jackbox.txt       the bypass domain list
  tools/
    edit_ui_strings.py           interface text editor (RU/EN side by side)
    build_content.py             editor for the version history and Info screen
    build_portable.py            builds the portable release (what ships on Releases)
```

`config.yaml` is created the first time a setting is saved. Everything in the interface
lives there too - editable as plain text; the program validates it on load and refuses
anything obviously unsafe.

Editing interface text or the Info screen's content through `tools/` doesn't need a
manual rebuild - `run.bat` notices the source is newer than the built interface and
rebuilds it on the next launch by itself.

---

## Security and privacy

- **Administrator rights** are needed for exactly two things: Zapret loads the WinDivert
  driver into the kernel, and the certificate goes into the system store. Nothing else.
- **The bridge only listens on `127.0.0.1`**, on two ports: the main one (8443 by
  default) and a service port for the Blobcast session (38203 by default, only opened
  while interception is on). Neither is reachable from the local network or the
  internet - only a program on this same computer can connect.
- **The certificate** is created locally and signs `localhost`. The private key is
  restricted to the current user, SYSTEM, and administrators - the same key terminates
  Blobcast's TLS sessions.
- **Logs are redacted**: tokens, passwords, and user identifiers, both in the file and
  in the Logs screen. Authorization headers are hidden entirely.
- **No telemetry.** The program sends nothing about you anywhere; every outbound request
  is your own game talking to Jackbox's servers.
- **Where traffic goes** is set by the active profile's address in Connection profiles,
  and only accepts `https://` - a guard against an address being swapped for someone
  else's server.
- **Profile import** treats the file as untrusted input: it can only *add* profiles,
  never overwrites existing ones, never changes which one is active, and can't replace
  an official server (official profiles are ignored on import entirely). Details in
  `backend/bridgebox/profiles_io.py`.
- **The portable build writes nothing outside its own folder.** No `%APPDATA%`, no
  registry (aside from the optional Task Scheduler entry, and only if you turned
  autostart on yourself) - see [Download](#download).

---

## Support the project

BridgeBox is free and stays that way - the license flat out forbids selling it or
bundling it into a paid product. If you want to support development with money, there
are three options (same links as the program's own Info screen):

- **[Donatty](https://donatty.com/bridgebox)** - for Russia.
- **USDT**: `ERC20` `0xda7a456c3a48d1b607fc17b8c085c049a7221693`, `TRC20`
  `TLWSMZqmsak8xnr9qTgQVmRiWCzE6mQGY4`.
- **Bitcoin**: `18qBmbhKyDNrrrwsMcyq2niZ8dKK9MEJuL`.

Donators, bug hunters, and testers who've contributed show up in the "Thanks" section
on the Info screen, along with what each of them found or checked.

---

## License

BridgeBox is distributed under [PolyForm Noncommercial 1.0.0](LICENSE.md): free to use,
modify, and share, not to sell or bundle into a paid product.

Third-party components are licensed separately - the full list with authors and links
is in [CREDITS.md](CREDITS.md) and on the program's own Info screen (the "Show list"
button next to the license line). The main ones: **zapret** (bol-van), the
**zapret-discord-youtube** build (Flowseal), **WinDivert** (basil00), **aiohttp**,
**pywebview**, **React**, and the **Inter**, **Manrope**, and **JetBrains Mono** fonts
under the SIL OFL 1.1.
