"""The two pages a human sees if they point a browser at the bridge.

Self-contained by necessity, not by preference: no external CSS, no webfont,
no image request. This program exists for networks where an outbound request
to a CDN is exactly what does not work, and the bridge serves no static assets
of its own - `frontend/dist` is loaded by pywebview over file://, not by this
server. Everything is inline.

The mark is the bridge glyph from components/icons.tsx (IconBridge), the same
one the setup wizard opens with. Deliberately not the full BrandLogo wordmark:
that is 16 KB of path data, and a second copy of it here would drift from the
real one the moment either changed.
"""
from __future__ import annotations

from .. import i18n

# Values lifted from styles/tokens.css rather than invented, so these pages
# look like the app rather than like a server error page.
_MARK = (
    '<svg viewBox="0 0 20 20" fill="none" aria-hidden="true" width="52" height="52">'
    '<path d="M1.5 13.5h17M4.5 13.5V6M15.5 13.5V6M1.5 9.5c1.6 0 2.4-3.5 3-3.5'
    'M18.5 9.5c-1.6 0-2.4-3.5-3-3.5M4.5 6c1.9 0 3.6 3 5.5 3s3.6-3 5.5-3M10 13.5V9" '
    'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>'
    "</svg>"
)

_BASE_CSS = """
:root { color-scheme: dark light; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh;
  display: flex; align-items: center; justify-content: center;
  padding: 32px;
  background: #0a0f1e; color: #e8eef8;
  font-family: 'Segoe UI Variable', 'Segoe UI', system-ui, sans-serif;
  line-height: 1.5;
}
main { max-width: 30rem; text-align: center; }
.mark {
  display: grid; place-items: center;
  width: 84px; height: 84px; margin: 0 auto 24px;
  border-radius: 999px;
}
h1 {
  margin: 0 0 12px;
  font-size: 26px; font-weight: 700; letter-spacing: -0.022em;
}
p { margin: 0 0 16px; font-size: 14px; color: #9aa6bd; }
p:last-child { margin-bottom: 0; }
code {
  font-family: 'Cascadia Mono', Consolas, monospace;
  font-size: 13px; padding: 1px 5px; border-radius: 6px;
  background: #17233d; color: #e8eef8;
}
@media (prefers-color-scheme: light) {
  body { background: #f8fafc; color: #0f172a; }
  p { color: #5b6472; }
  code { background: #e6edff; color: #0f172a; }
}
"""

_LANDING_CSS = """
.mark { background: #17233d; color: #60a5fa; }
@media (prefers-color-scheme: light) {
  .mark { background: #e6edff; color: #1d4ed8; }
}
"""

# Red, and heavier: this one is telling somebody to stop, not explaining a
# curiosity. Same layout so it reads as the same program.
_SERVICE_CSS = """
.mark { background: #2a1518; color: #f87171; }
h1 { color: #f87171; }
.rule {
  margin: 24px 0 0; padding-top: 16px;
  border-top: 1px solid #2a1518;
  font-size: 13px; color: #7d8798;
}
@media (prefers-color-scheme: light) {
  .mark { background: #fee2e2; color: #b91c1c; }
  h1 { color: #b91c1c; }
  .rule { border-top-color: #fee2e2; }
}
"""


def _page(lang: str, title: str, extra_css: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        f'<html lang="{lang}">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n"
        f"<style>{_BASE_CSS}{extra_css}</style>\n"
        "</head>\n<body>\n<main>\n"
        f'<div class="mark">{_MARK}</div>\n'
        f"{body}\n"
        "</main>\n</body>\n</html>\n"
    )


def landing_page(lang: str = "ru") -> str:
    """Somebody typed the bridge address into a browser out of curiosity."""
    t = i18n.t
    return _page(
        lang,
        t("pages.landing_title", lang),
        _LANDING_CSS,
        f"<h1>{t('pages.landing_heading', lang)}</h1>\n"
        f"<p>{t('pages.landing_body1', lang)}</p>\n"
        f"<p>{t('pages.landing_body2', lang)}</p>\n"
        f"<p>{t('pages.landing_body3', lang)}</p>",
    )


def service_page(path: str, lang: str = "ru") -> str:
    """Somebody opened a path the game uses to talk to Jackbox.

    A browser request here is not harmless the way the landing page is: the
    bridge would forward it upstream under the game's own identity, so an
    idle refresh becomes a real request to Jackbox's servers - which is how
    room-creation endpoints get hit by something that will never play a game,
    and how an address ends up rate-limited."""
    safe_path = path.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = i18n.t
    return _page(
        lang,
        t("pages.service_title", lang),
        _SERVICE_CSS,
        f"<h1>{t('pages.service_heading', lang)}</h1>\n"
        f"<p>{t('pages.service_body1', lang, path=safe_path)}</p>\n"
        f"<p>{t('pages.service_body2', lang)}</p>\n"
        f'<p class="rule">{t("pages.service_footer", lang)}</p>',
    )
