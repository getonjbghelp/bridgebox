// Mirrors the project-root credits.json/CREDITS.md by hand - there is no
// bridge call backing this; keep all three in sync manually when adding an
// entry. Unlike those two (English-only, for anyone reading the repo
// directly), purpose is bilingual here - this is the one copy the running
// app actually renders, in whichever locale the user picked (see
// ThirdPartyLicenses.tsx's entry.purpose[locale]).
import type { Locale } from '../state/MotionPrefsContext'

export interface CreditEntry {
  name: string
  author: string
  authorUrl: string
  projectUrl: string
  license: string
  purpose: Record<Locale, string>
}

export const CREDITS: CreditEntry[] = [
  {
    name: 'zapret-discord-youtube',
    author: 'Flowseal',
    authorUrl: 'https://github.com/Flowseal',
    projectUrl: 'https://github.com/Flowseal/zapret-discord-youtube',
    license: 'MIT',
    purpose: {
      ru: 'Windows packaging, DPI-desync strategy .bat файлы и general.bat baseline, адаптированные в zapret/',
      en: 'Windows packaging, DPI-desync strategy .bat files and general.bat baseline adapted in zapret/ for routing Jackbox traffic around DPI',
    },
  },
  {
    name: 'zapret',
    author: 'bol-van',
    authorUrl: 'https://github.com/bol-van',
    projectUrl: 'https://github.com/bol-van/zapret',
    license: 'MIT',
    purpose: {
      ru: 'Оригинальный DPI-desync движок (ядро winws.exe)',
      en: "Original DPI-desync engine (winws.exe core) that Flowseal's Windows distribution bundles",
    },
  },
  {
    name: 'WinDivert',
    author: 'basil (basil00)',
    authorUrl: 'https://github.com/basil00',
    projectUrl: 'https://github.com/basil00/Divert',
    license: 'LGPL-3.0 OR GPL-2.0',
    purpose: {
      ru: 'Перехват/инъекция пакетов на уровне ядра для winws.exe',
      en: 'Kernel-level packet interception/injection (WinDivert.dll, WinDivert64.sys) that winws.exe uses to apply DPI-desync',
    },
  },
  {
    name: 'aiohttp',
    author: 'aio-libs contributors',
    authorUrl: 'https://github.com/aio-libs',
    projectUrl: 'https://github.com/aio-libs/aiohttp',
    license: 'Apache-2.0',
    purpose: {
      ru: 'HTTPS + WebSocket сервер и клиент моста',
      en: 'Async HTTPS + WebSocket server and client used by the BridgeBox Ecast bridge',
    },
  },
  {
    name: 'cryptography',
    author: 'Python Cryptographic Authority',
    authorUrl: 'https://github.com/pyca',
    projectUrl: 'https://github.com/pyca/cryptography',
    license: 'Apache-2.0 OR BSD-3-Clause',
    purpose: {
      ru: 'Локальный CA и TLS-сертификат для localhost',
      en: "Local CA and localhost TLS certificate generation for the bridge's HTTPS/WSS endpoint",
    },
  },
  {
    name: 'pydantic',
    author: 'Samuel Colvin and contributors',
    authorUrl: 'https://github.com/pydantic',
    projectUrl: 'https://github.com/pydantic/pydantic',
    license: 'MIT',
    purpose: {
      ru: 'Валидация схемы конфигурации',
      en: 'Config schema validation (config.yaml)',
    },
  },
  {
    name: 'PyYAML',
    author: 'Kirill Simonov and contributors',
    authorUrl: 'https://github.com/yaml',
    projectUrl: 'https://github.com/yaml/pyyaml',
    license: 'MIT',
    purpose: {
      ru: 'Разбор config.yaml',
      en: 'YAML parsing for config.yaml',
    },
  },
  {
    name: 'React',
    author: 'Meta and contributors',
    authorUrl: 'https://github.com/facebook',
    projectUrl: 'https://github.com/facebook/react',
    license: 'MIT',
    purpose: {
      ru: 'UI-библиотека интерфейса BridgeBox',
      en: 'UI library powering the BridgeBox interface',
    },
  },
  {
    name: 'Framer Motion',
    author: 'Framer',
    authorUrl: 'https://github.com/framer',
    projectUrl: 'https://github.com/framer/motion',
    license: 'MIT',
    purpose: {
      ru: 'Пружинная физика и переходы интерфейса',
      en: 'Spring physics and transitions across the UI',
    },
  },
  {
    name: 'pywebview',
    author: 'Roman Sirokov and contributors',
    authorUrl: 'https://github.com/r0x0r',
    projectUrl: 'https://github.com/r0x0r/pywebview',
    license: 'BSD-3-Clause',
    purpose: {
      ru: 'Нативное окно десктоп-приложения вокруг веб-интерфейса',
      en: 'Native desktop window hosting the web-based UI',
    },
  },
  {
    name: 'Inter',
    author: 'Rasmus Andersson (rsms)',
    authorUrl: 'https://github.com/rsms',
    projectUrl: 'https://github.com/rsms/inter',
    license: 'OFL-1.1',
    purpose: {
      ru: 'Основной шрифт интерфейса, вшит подмножеством Latin+Cyrillic',
      en: 'Body and UI typeface, vendored as a Latin+Cyrillic subset in frontend/src/assets/fonts/',
    },
  },
  {
    name: 'Manrope',
    author: 'Mikhail Sharanda',
    authorUrl: 'https://github.com/sharanda',
    projectUrl: 'https://github.com/sharanda/manrope',
    license: 'OFL-1.1',
    purpose: {
      ru: 'Заголовочный шрифт, вшит подмножеством Latin+Cyrillic',
      en: 'Display typeface (headings, wordmark), vendored as a Latin+Cyrillic subset in frontend/src/assets/fonts/',
    },
  },
  {
    name: 'JetBrains Mono',
    author: 'JetBrains',
    authorUrl: 'https://github.com/JetBrains',
    projectUrl: 'https://github.com/JetBrains/JetBrainsMono',
    license: 'OFL-1.1',
    purpose: {
      ru: 'Моноширинный шрифт: адреса, порты, строки логов, тайминги',
      en: 'Monospace typeface (addresses, ports, log lines, timings), vendored as a Latin+Cyrillic subset in frontend/src/assets/fonts/',
    },
  },
  {
    name: 'PyInstaller',
    author: 'PyInstaller Development Team',
    authorUrl: 'https://github.com/pyinstaller',
    projectUrl: 'https://github.com/pyinstaller/pyinstaller',
    // A license identifier's own parenthetical stays in English regardless of
    // locale, same as "MIT"/"Apache-2.0" above never get translated either.
    license: 'GPL-2.0-or-later (with an exception for programs it builds)',
    purpose: {
      ru: 'Собирает backend в portable .exe для сборки - его bootloader вшит в готовый бинарник, сам код BridgeBox под эту лицензию не подпадает',
      en: "Packages the backend into the portable .exe for release - its bootloader is embedded in the resulting binary; BridgeBox's own code is not covered by this license",
    },
  },
  {
    name: 'johnbox',
    author: 'InvoxiPlayGames',
    authorUrl: 'https://github.com/InvoxiPlayGames',
    projectUrl: 'https://github.com/InvoxiPlayGames/johnbox',
    license: 'AGPL-3.0',
    purpose: {
      ru: 'Только референс, код не заимствован - community-сервер Ecast API v2, независимо подтвердивший формат host/code/token и произвольный путь WS-подключения',
      en: "Reference only, no code bundled or derived - a community Ecast API v2 server whose room-creation response independently confirmed the real host/code/token shape (vs. the project's own unverified research docs) and that the game's WebSocket handshake isn't tied to a fixed path",
    },
  },
  {
    name: 'tg-ws-proxy',
    author: 'Flowseal',
    authorUrl: 'https://github.com/Flowseal',
    projectUrl: 'https://github.com/Flowseal/tg-ws-proxy',
    license: 'MIT',
    purpose: {
      ru: 'Только референс, код не заимствован - паттерн прогрева пула WS-соединений (proxy/pool.py) вдохновил прогрев подключения к Ecast в runtime_core.py, чтобы создание комнаты не платило за холодный TCP+TLS хендшейк поверх задержки zapret',
      en: "Reference only, no code bundled or derived - its WS connection pool warmup (proxy/pool.py) inspired pre-warming the Ecast upstream connection in runtime_core.py, so room creation doesn't pay a cold TCP+TLS handshake on top of zapret's own overhead",
    },
  },
]
