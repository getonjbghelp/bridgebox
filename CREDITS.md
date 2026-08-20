# Credits

## zapret-discord-youtube
- Author: [Flowseal](https://github.com/Flowseal)
- Project: https://github.com/Flowseal/zapret-discord-youtube
- License: MIT
- Used for: Windows packaging, DPI-desync strategy .bat files and general.bat baseline adapted in zapret/ for routing Jackbox traffic around DPI

## zapret
- Author: [bol-van](https://github.com/bol-van)
- Project: https://github.com/bol-van/zapret
- License: MIT
- Used for: Original DPI-desync engine (winws.exe core) that Flowseal's Windows distribution bundles

## WinDivert
- Author: [basil (basil00)](https://github.com/basil00)
- Project: https://github.com/basil00/Divert
- License: LGPL-3.0 OR GPL-2.0
- Used for: Kernel-level packet interception/injection (WinDivert.dll, WinDivert64.sys) that winws.exe uses to apply DPI-desync

## aiohttp
- Author: [aio-libs contributors](https://github.com/aio-libs)
- Project: https://github.com/aio-libs/aiohttp
- License: Apache-2.0
- Used for: Async HTTPS + WebSocket server and client used by the BridgeBox Ecast bridge

## cryptography
- Author: [Python Cryptographic Authority](https://github.com/pyca)
- Project: https://github.com/pyca/cryptography
- License: Apache-2.0 OR BSD-3-Clause
- Used for: Local CA and localhost TLS certificate generation for the bridge's HTTPS/WSS endpoint

## pydantic
- Author: [Samuel Colvin and contributors](https://github.com/pydantic)
- Project: https://github.com/pydantic/pydantic
- License: MIT
- Used for: Config schema validation (config.yaml)

## PyYAML
- Author: [Kirill Simonov and contributors](https://github.com/yaml)
- Project: https://github.com/yaml/pyyaml
- License: MIT
- Used for: YAML parsing for config.yaml

## React
- Author: [Meta and contributors](https://github.com/facebook)
- Project: https://github.com/facebook/react
- License: MIT
- Used for: UI library powering the BridgeBox interface

## Framer Motion
- Author: [Framer](https://github.com/framer)
- Project: https://github.com/framer/motion
- License: MIT
- Used for: Spring physics and transitions across the UI

## pywebview
- Author: [Roman Sirokov and contributors](https://github.com/r0x0r)
- Project: https://github.com/r0x0r/pywebview
- License: BSD-3-Clause
- Used for: Native desktop window hosting the web-based UI

## Inter
- Author: [Rasmus Andersson (rsms)](https://github.com/rsms)
- Project: https://github.com/rsms/inter
- License: OFL-1.1
- Used for: Body and UI typeface, vendored as a Latin+Cyrillic subset in frontend/src/assets/fonts/

## Manrope
- Author: [Mikhail Sharanda](https://github.com/sharanda)
- Project: https://github.com/sharanda/manrope
- License: OFL-1.1
- Used for: Display typeface (headings, wordmark), vendored as a Latin+Cyrillic subset in frontend/src/assets/fonts/

## JetBrains Mono
- Author: [JetBrains](https://github.com/JetBrains)
- Project: https://github.com/JetBrains/JetBrainsMono
- License: OFL-1.1
- Used for: Monospace typeface (addresses, ports, log lines, timings), vendored as a Latin+Cyrillic subset in frontend/src/assets/fonts/

## PyInstaller
- Author: [PyInstaller Development Team](https://github.com/pyinstaller)
- Project: https://github.com/pyinstaller/pyinstaller
- License: GPL-2.0-or-later (with an exception for programs it builds)
- Used for: Packages the backend into the portable .exe for release - its bootloader is embedded in the resulting binary; BridgeBox's own code is not covered by this license

## johnbox
- Author: [InvoxiPlayGames](https://github.com/InvoxiPlayGames)
- Project: https://github.com/InvoxiPlayGames/johnbox
- License: AGPL-3.0
- Used for: Reference only, no code bundled or derived - a community Ecast API v2 server whose room-creation response independently confirmed the real host/code/token shape (vs. the project's own unverified research docs) and that the game's WebSocket handshake isn't tied to a fixed path
