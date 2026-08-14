// Mirrors the project-root credits.json/CREDITS.md by hand - there is no
// bridge call backing this; keep all three in sync manually when adding an entry.
export interface CreditEntry {
  name: string
  author: string
  authorUrl: string
  projectUrl: string
  license: string
  purpose: string
}

export const CREDITS: CreditEntry[] = [
  {
    name: 'zapret-discord-youtube',
    author: 'Flowseal',
    authorUrl: 'https://github.com/Flowseal',
    projectUrl: 'https://github.com/Flowseal/zapret-discord-youtube',
    license: 'MIT',
    purpose:
      'Windows packaging, DPI-desync strategy .bat файлы и general.bat baseline, адаптированные в zapret/',
  },
  {
    name: 'zapret',
    author: 'bol-van',
    authorUrl: 'https://github.com/bol-van',
    projectUrl: 'https://github.com/bol-van/zapret',
    license: 'MIT',
    purpose: 'Оригинальный DPI-desync движок (ядро winws.exe)',
  },
  {
    name: 'WinDivert',
    author: 'basil (basil00)',
    authorUrl: 'https://github.com/basil00',
    projectUrl: 'https://github.com/basil00/Divert',
    license: 'LGPL-3.0 OR GPL-2.0',
    purpose: 'Перехват/инъекция пакетов на уровне ядра для winws.exe',
  },
  {
    name: 'aiohttp',
    author: 'aio-libs contributors',
    authorUrl: 'https://github.com/aio-libs',
    projectUrl: 'https://github.com/aio-libs/aiohttp',
    license: 'Apache-2.0',
    purpose: 'HTTPS + WebSocket сервер и клиент моста',
  },
  {
    name: 'cryptography',
    author: 'Python Cryptographic Authority',
    authorUrl: 'https://github.com/pyca',
    projectUrl: 'https://github.com/pyca/cryptography',
    license: 'Apache-2.0 OR BSD-3-Clause',
    purpose: 'Локальный CA и TLS-сертификат для localhost',
  },
  {
    name: 'pydantic',
    author: 'Samuel Colvin and contributors',
    authorUrl: 'https://github.com/pydantic',
    projectUrl: 'https://github.com/pydantic/pydantic',
    license: 'MIT',
    purpose: 'Валидация схемы конфигурации',
  },
  {
    name: 'PyYAML',
    author: 'Kirill Simonov and contributors',
    authorUrl: 'https://github.com/yaml',
    projectUrl: 'https://github.com/yaml/pyyaml',
    license: 'MIT',
    purpose: 'Разбор config.yaml',
  },
  {
    name: 'React',
    author: 'Meta and contributors',
    authorUrl: 'https://github.com/facebook',
    projectUrl: 'https://github.com/facebook/react',
    license: 'MIT',
    purpose: 'UI-библиотека интерфейса BridgeBox',
  },
  {
    name: 'Framer Motion',
    author: 'Framer',
    authorUrl: 'https://github.com/framer',
    projectUrl: 'https://github.com/framer/motion',
    license: 'MIT',
    purpose: 'Пружинная физика и переходы интерфейса',
  },
  {
    name: 'pywebview',
    author: 'Roman Sirokov and contributors',
    authorUrl: 'https://github.com/r0x0r',
    projectUrl: 'https://github.com/r0x0r/pywebview',
    license: 'BSD-3-Clause',
    purpose: 'Нативное окно десктоп-приложения вокруг веб-интерфейса',
  },
  {
    name: 'Inter',
    author: 'Rasmus Andersson (rsms)',
    authorUrl: 'https://github.com/rsms',
    projectUrl: 'https://github.com/rsms/inter',
    license: 'OFL-1.1',
    purpose: 'Основной шрифт интерфейса, вшит подмножеством Latin+Cyrillic',
  },
  {
    name: 'Manrope',
    author: 'Mikhail Sharanda',
    authorUrl: 'https://github.com/sharanda',
    projectUrl: 'https://github.com/sharanda/manrope',
    license: 'OFL-1.1',
    purpose: 'Заголовочный шрифт, вшит подмножеством Latin+Cyrillic',
  },
  {
    name: 'JetBrains Mono',
    author: 'JetBrains',
    authorUrl: 'https://github.com/JetBrains',
    projectUrl: 'https://github.com/JetBrains/JetBrainsMono',
    license: 'OFL-1.1',
    purpose: 'Моноширинный шрифт: адреса, порты, строки логов, тайминги',
  },
  {
    name: 'johnbox',
    author: 'InvoxiPlayGames',
    authorUrl: 'https://github.com/InvoxiPlayGames',
    projectUrl: 'https://github.com/InvoxiPlayGames/johnbox',
    license: 'AGPL-3.0',
    purpose:
      'Только референс, код не заимствован - community-сервер Ecast API v2, независимо подтвердивший формат host/code/token и произвольный путь WS-подключения',
  },
]
