"""Text for the handful of things the backend itself shows a person.

This is NOT the app's UI text - that lives in frontend/src/data/strings/
{ru,en}.json and never touches Python. This module covers the few surfaces
the frontend cannot reach at all: the native tray menu (WinForms, not a web
view), the pages a browser gets if it's pointed at the bridge directly
(server/pages.py), and the handful of authored error messages a user is
actually meant to read (as opposed to the free-form log output the Logs
screen shows, which stays as-is - see the language architecture discussion
this module came out of).

Two languages, a few dozen keys - gettext/Babel would be solving a problem
this app does not have. A dict and str.format() are the whole mechanism.
"""
from __future__ import annotations

import locale as _locale

Locale = str  # "ru" | "en"
LOCALES: tuple[Locale, ...] = ("ru", "en")


def detect_system_locale() -> Locale:
    """The OS's own language, collapsed to one this module has text for.

    Mirrors the frontend's navigator.language check (MotionPrefsContext.tsx)
    for the same reason: this only runs as the fallback for `language:
    "system"`, and each side has to ask its own platform rather than one
    trying to guess the other's answer."""
    try:
        lang = _locale.getlocale()[0] or ""
    except Exception:
        lang = ""
    return "ru" if lang.lower().startswith("ru") else "en"


def resolve_locale(preference: str) -> Locale:
    """UiConfig.language ("system"/"ru"/"en") -> an actual Locale.

    Unrecognised values resolve the same as "system" rather than raising -
    config.yaml can be hand-edited, and a typo there should not crash the
    tray icon."""
    return preference if preference in LOCALES else detect_system_locale()


# Keyed by "area.name" for the same reason the frontend's strings.json is
# grouped by screen - not enforced, just legible when this grows.
_MESSAGES: dict[str, dict[Locale, str]] = {
    "tray.show": {"ru": "Открыть BridgeBox", "en": "Open BridgeBox"},
    "tray.stop_bridge": {"ru": "Остановить мост", "en": "Stop the bridge"},
    "tray.quit": {"ru": "Выход", "en": "Quit"},
    "tray.tooltip_running": {
        "ru": "{title} — мост включён",
        "en": "{title} — bridge is running",
    },
    "tray.tooltip_stopped": {
        "ru": "{title} — мост выключен",
        "en": "{title} — bridge is stopped",
    },
    "pages.landing_title": {"ru": "BridgeBox", "en": "BridgeBox"},
    "pages.landing_heading": {"ru": "BridgeBox", "en": "BridgeBox"},
    "pages.landing_body1": {
        "ru": (
            "Это локальный мост BridgeBox. Он работает только на вашем "
            "компьютере и существует для одного: принимать трафик вашей игры "
            "Jackbox и вести его в обход блокировок."
        ),
        "en": (
            "This is BridgeBox's local bridge. It only runs on this "
            "computer, and it exists to do one thing: take your Jackbox "
            "game's traffic and route it around network blocks."
        ),
    },
    "pages.landing_body2": {
        "ru": "Смотреть тут нечего — у моста нет веб-интерфейса. Управление находится в самом приложении BridgeBox.",
        "en": "There's nothing to see here — the bridge has no web interface. Everything is controlled from the BridgeBox app itself.",
    },
    "pages.landing_body3": {
        "ru": "Эта страница открылась потому, что вы зашли сюда браузером. Игра получает совсем другие ответы.",
        "en": "You're seeing this page because a browser opened this address. The game itself gets a completely different response.",
    },
    "pages.service_title": {
        "ru": "BridgeBox — служебный адрес",
        "en": "BridgeBox — internal address",
    },
    "pages.service_heading": {"ru": "Так делать не нужно", "en": "This isn't meant to be opened"},
    "pages.service_body1": {
        "ru": (
            "Адрес <code>{path}</code> — служебный. По нему игра общается с "
            "серверами Jackbox через мост, и запрос отсюда уйдёт наверх как "
            "настоящий игровой."
        ),
        "en": (
            "<code>{path}</code> is an internal address. The game uses it to "
            "talk to Jackbox's servers through the bridge, and a request "
            "from here would be forwarded upstream as if it came from the "
            "game itself."
        ),
    },
    "pages.service_body2": {
        "ru": (
            "Обновление страницы в браузере — это не просмотр, а реальное "
            "обращение к Jackbox от вашего имени. Так создаются пустые "
            "комнаты и так адрес попадает под ограничение частоты запросов."
        ),
        "en": (
            "Reloading this in a browser isn't just viewing it — it's a "
            "real request to Jackbox on your behalf. That's how empty rooms "
            "get created and how an address ends up rate-limited."
        ),
    },
    "pages.service_footer": {
        "ru": "Запрос не был отправлен дальше. Закройте вкладку и пользуйтесь приложением BridgeBox.",
        "en": "This request was not forwarded. Close this tab and use the BridgeBox app instead.",
    },
    "update.apply_failed_partial": {
        "ru": (
            "обновление применено наполовину: не удалось вернуть {files}. "
            "Рядом с ними лежат файлы .bak - восстановите их вручную или "
            "переустановите BridgeBox"
        ),
        "en": (
            "the update only partly applied: could not restore {files}. "
            "Their .bak backups are still sitting next to them - restore "
            "them by hand or reinstall BridgeBox"
        ),
    },
    "update.apply_failed_rolled_back": {
        "ru": (
            "файл занят другим процессом, обновление отменено и всё "
            "возвращено как было. Закройте игру и другие программы, "
            "использующие zapret, либо перезагрузите компьютер, и повторите"
        ),
        "en": (
            "a file was locked by another process, so the update was "
            "cancelled and everything was put back the way it was. Close "
            "the game and anything else using zapret, or restart your "
            "computer, then try again"
        ),
    },
    "update.apply_failed_cause": {
        "ru": "{detail}. Причина: {cause}",
        "en": "{detail}. Cause: {cause}",
    },
    # "Проверить соединение (ping)" on the home screen - Api._test_connection_coro
    # and _close_test_room build these into the `steps` list the frontend
    # renders verbatim, so they have to be resolved backend-side rather than
    # left for the frontend to translate after the fact.
    "diag.no_ca_file": {
        "ru": "внимание: не найден {name}, сертификат не проверяется",
        "en": "warning: {name} not found, the certificate is not being verified",
    },
    "diag.bridge_not_running": {
        "ru": "мост не запущен",
        "en": "the bridge is not running",
    },
    "diag.ping_ok": {
        "ru": "пинг {name}: OK (HTTP {status}, {ms} мс)",
        "en": "ping {name}: OK (HTTP {status}, {ms} ms)",
    },
    "diag.ping_error": {
        "ru": "пинг {name}: ошибка ({error})",
        "en": "ping {name}: error ({error})",
    },
    "diag.create_room_network_error": {
        "ru": "создание комнаты: ошибка сети ({detail})",
        "en": "room creation: network error ({detail})",
    },
    "diag.create_room_not_json": {
        "ru": "создание комнаты: HTTP {status}, ответ не JSON ({n} байт): {snippet}",
        "en": "room creation: HTTP {status}, response is not JSON ({n} bytes): {snippet}",
    },
    "diag.create_room_unexpected_json": {
        "ru": "создание комнаты: HTTP {status}, неожиданный JSON: {json}",
        "en": "room creation: HTTP {status}, unexpected JSON: {json}",
    },
    "diag.create_room_no_code": {
        "ru": (
            "создание комнаты: HTTP {status}, не нашли код комнаты среди "
            "{keys} ни на одном уровне вложенности: {json}"
        ),
        "en": (
            "room creation: HTTP {status}, no room code found among "
            "{keys} at any nesting level: {json}"
        ),
    },
    "diag.room_created": {
        "ru": "комната {room_id} создана (HTTP {status}){relay_note}",
        "en": "room {room_id} created (HTTP {status}){relay_note}",
    },
    "diag.room_check_network_error": {
        "ru": "проверка комнаты {room_id}: ошибка сети ({detail})",
        "en": "checking room {room_id}: network error ({detail})",
    },
    "diag.room_check_bad_status": {
        "ru": "проверка комнаты {room_id}: HTTP {status}",
        "en": "checking room {room_id}: HTTP {status}",
    },
    "diag.room_confirmed": {
        "ru": "комната {room_id} подтверждена (GET /api/v2/rooms/{room_id} -> HTTP 200)",
        "en": "room {room_id} confirmed (GET /api/v2/rooms/{room_id} -> HTTP 200)",
    },
    "diag.room_close_failed_network": {
        "ru": "комната {room_id}: закрыть не удалось ({detail})",
        "en": "room {room_id}: could not close it ({detail})",
    },
    "diag.room_closed": {
        "ru": "комната {room_id} закрыта (DELETE -> HTTP {status})",
        "en": "room {room_id} closed (DELETE -> HTTP {status})",
    },
    "diag.room_close_failed_status": {
        "ru": "комната {room_id}: закрыть не удалось (HTTP {status}: {detail})",
        "en": "room {room_id}: could not close it (HTTP {status}: {detail})",
    },
}


def t(key: str, lang: str, **kwargs: object) -> str:
    """Look up `key` in `lang`, falling back to Russian for a key that has
    not been translated yet rather than raising - a missing English string
    should read oddly, not take down whatever was about to show it."""
    table = _MESSAGES[key]
    text = table.get(lang) or table["ru"]
    return text.format(**kwargs) if kwargs else text
