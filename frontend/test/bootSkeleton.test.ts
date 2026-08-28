// Self-check for the boot skeleton in index.html. Run it with:
//
//   node --test test/bootSkeleton.test.ts        (from frontend/)
//
// What this guards: the skeleton has to paint before the module bundle is even
// fetched, which is the only way to cover WebView2 start-up and the parse of a
// 433KB bundle - no React code has run at that point. That constraint forces
// two things that a normal component would never need, and both rot silently:
//
//   1. Its colours are COPIED from tokens.css rather than imported, because a
//      stylesheet would be another round trip. Two shades off and the handover
//      to React reads as a flash.
//   2. Its markup and styles must come before the <script>, or the browser has
//      nothing to paint until the bundle arrives - which is the whole problem.
//
// Neither shows up in a typecheck, a lint or a build.

import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = fileURLToPath(new URL('.', import.meta.url))
const html = readFileSync(here + '../index.html', 'utf8')
const tokens = readFileSync(here + '../src/styles/tokens.css', 'utf8')

/** Resolve a token to its literal hex, following var() aliases. */
function token(name: string, scope: 'light' | 'dark'): string {
  // tokens.css declares the light palette on bare :root and overrides it in a
  // :root[data-theme='dark'] block, so the scope decides which definition of a
  // SEMANTIC token wins. The primitive scale it aliases (--navy-950 and
  // friends) is declared once, above both, so a lookup that misses in the dark
  // region falls back to the whole file rather than failing.
  const darkAt = tokens.indexOf("[data-theme='dark']")
  const region = scope === 'light' ? tokens.slice(0, darkAt) : tokens.slice(darkAt)
  const find = (text: string) => [...text.matchAll(new RegExp(`${name}:\\s*([^;]+);`, 'g'))]
  const matches = find(region).length > 0 ? find(region) : find(tokens)
  assert.ok(matches.length > 0, `${name} not found in the ${scope} palette`)
  const value = matches[matches.length - 1][1].trim()
  const alias = value.match(/^var\((--[\w-]+)\)$/)
  return alias ? token(alias[1], scope) : value
}

function bootVar(name: string, scope: 'light' | 'dark'): string {
  const darkAt = html.indexOf("[data-theme='dark']")
  const region = scope === 'light' ? html.slice(0, darkAt) : html.slice(darkAt)
  const match = region.match(new RegExp(`${name}:\\s*([^;]+);`))
  assert.ok(match, `${name} not found in the ${scope} skeleton block`)
  return match[1].trim().toLowerCase()
}

function opacityTransition(selector: string): { duration: number; curve: number[] } {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const rule = html.match(new RegExp(`${escaped}\\s*\\{[^}]*transition:\\s*opacity\\s+(\\d+)ms\\s+cubic-bezier\\(([^)]+)\\)`, 's'))
  assert.ok(rule, `${selector} opacity transition not found`)
  return {
    duration: Number(rule[1]),
    curve: rule[2].split(',').map((value) => Number(value.trim())),
  }
}

function bezierProgressAt(x: number, [x1, y1, x2, y2]: number[]): number {
  const point = (t: number, a: number, b: number) =>
    3 * (1 - t) ** 2 * t * a + 3 * (1 - t) * t ** 2 * b + t ** 3
  let low = 0
  let high = 1
  for (let i = 0; i < 30; i += 1) {
    const mid = (low + high) / 2
    if (point(mid, x1, x2) < x) low = mid
    else high = mid
  }
  return point((low + high) / 2, y1, y2)
}

test('the skeleton background matches the real one in both themes', () => {
  // A mismatch is not a style nit: it is a visible flash at the exact moment
  // the app is trying to look like it started instantly.
  assert.equal(bootVar('--bb-boot-bg', 'light'), token('--color-bg', 'light').toLowerCase())
  assert.equal(bootVar('--bb-boot-bg', 'dark'), token('--color-bg', 'dark').toLowerCase())
})

test('the skeleton bar matches the border token in both themes', () => {
  assert.equal(bootVar('--bb-boot-fg', 'light'), token('--color-border', 'light').toLowerCase())
  assert.equal(bootVar('--bb-boot-fg', 'dark'), token('--color-border', 'dark').toLowerCase())
})

test('the skeleton is painted before the bundle is fetched', () => {
  const markup = html.indexOf('id="bb-boot"')
  const styles = html.indexOf('<style>')
  const bundle = html.indexOf('src="/src/main.tsx"')

  assert.ok(markup > -1 && styles > -1 && bundle > -1)
  assert.ok(styles < bundle, 'the skeleton styles must precede the module script')
  assert.ok(markup < bundle, 'the skeleton markup must precede the module script')
})

test('nothing blocking is loaded ahead of the skeleton', () => {
  // An external stylesheet or a classic <script> before it would delay the
  // very first paint, which is the one thing this markup exists to protect.
  const markup = html.indexOf('id="bb-boot"')
  const head = html.slice(0, markup)

  assert.ok(!head.includes('<link rel="stylesheet"'), 'no blocking stylesheet before the skeleton')
  assert.ok(
    !/<script(?![^>]*\btype="module")[^>]*\bsrc=/.test(head),
    'no blocking external script before the skeleton',
  )
})

test('the handoff remains visible through a busy WebView2 startup', () => {
  const root = opacityTransition('#root')
  const boot = opacityTransition('#bb-boot.bb-boot--done')

  assert.deepEqual(root, boot, 'both sides of the crossfade must use the same timing')
  assert.ok(root.duration >= 480, 'a short transition is swallowed by WebView2 startup work')
  assert.ok(
    bezierProgressAt(0.5, root.curve) <= 0.6,
    'a front-loaded curve turns a dropped startup frame into a hard cut',
  )
})

test('App.tsx is what takes the skeleton down', () => {
  // Removing it on mount would trade the skeleton for a blank window: App
  // renders nothing at all until setupComplete is known.
  const app = readFileSync(here + '../src/App.tsx', 'utf8')

  assert.ok(app.includes("getElementById('bb-boot')"), 'App.tsx must dismiss the skeleton')
  assert.ok(
    app.includes('if (setupComplete === null) return\n'),
    'the dismissal must wait for setupComplete',
  )
})
