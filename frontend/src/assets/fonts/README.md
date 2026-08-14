# Fonts

Three families, self-hosted, subset to Latin + Cyrillic. All SIL OFL 1.1 —
`OFL-*.txt` here are the vendored licences and must ship with the app.

| File | Family | Role | Size |
|---|---|---|---|
| `Manrope-subset.woff2` | Manrope 200–800 | display: headings, wordmark, power-button label | 29 KB |
| `Inter-subset.woff2` | Inter 100–900 (+ `opsz`) | everything else | 78 KB |
| `JetBrainsMono-subset.woff2` | JetBrains Mono 100–800 | addresses, ports, log lines, timings | 44 KB |

## Why these live in `src/assets/`, not `public/`

`vite.config.ts` sets `base: './'`, and pywebview loads `frontend/dist/index.html`
over `file://`. A file in `public/` is referenced by a root-absolute `/fonts/…`
URL, which under `file://` resolves to the **drive root** and 404s — silently,
falling back to a system font. That failure appears only in the packaged app,
never in `npm run dev`. Assets under `src/` go through Vite, land in
`dist/assets/` and are referenced relatively from the emitted CSS.

## Why not Google Fonts

This app exists to get around DPI blocking. A `@import` from
`fonts.googleapis.com` would be a network request to a host that may be exactly
what's blocked or throttled — at first paint, before the bridge is even running.

## Regenerating

Only needed when adding or updating a family. `subset.py` is dev-time only;
nothing in the app imports it.

```bash
pip install fonttools brotli
python subset.py <output-dir>
```

Sources (variable `.ttf`, weight axis):

- Inter — `github.com/rsms/inter`, `docs/font-files/InterVariable.ttf`
- Manrope — `github.com/google/fonts`, `ofl/manrope/Manrope[wght].ttf`
- JetBrains Mono — `github.com/JetBrains/JetBrainsMono`, `fonts/variable/JetBrainsMono[wght].ttf`

## Known gap

`✕` (U+2715) is not in Inter or Manrope — only JetBrains Mono has it. Status
badges use SVG icons from `components/icons.tsx` rather than text glyphs, so
this doesn't bite; don't reintroduce a bare `✕` in UI copy, it will silently
fall back to whatever system font has it.
