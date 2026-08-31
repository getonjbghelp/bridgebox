import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const here = fileURLToPath(new URL('.', import.meta.url))
const source = (path: string) => readFileSync(here + '../src/' + path, 'utf8')

test('screen transitions have no intermediate requestAnimationFrame flash', () => {
  const app = source('App.tsx')

  // The flash was a two-step activation: the screen was marked active on
  // one frame and "entered" on the next, so one committed frame in between
  // carried neither state's styles. The CSS animation replaying on its own
  // is what replaced it - there is no second step to get out of sync.
  assert.ok(!app.includes('enteredScreen'))
  assert.ok(!app.includes('requestAnimationFrame'))
})

test('an inactive screen takes up no space at all', () => {
  const css = source('App.css')

  // display:none, not a visibility/opacity stack. An inactive screen that
  // still generates a box occupies the same space as the active one, and
  // every version of that idea cost something real: a taller hidden screen
  // leaking its height into .bb-app__content's overflow-y:auto (a
  // scrollbar on a screen with nowhere to scroll), then the active screen
  // no longer able to scroll when it genuinely was too tall, then - once
  // the screens were positioned against .bb-app__content itself - the
  // content padding, because an absolutely positioned box resolves inset:0
  // against the padding box and sits over the padding rather than inside
  // it. None of those are reachable while an inactive screen has no box.
  assert.match(css, /\.bb-app__screen\s*\{[^}]*display:\s*none/s)
  assert.doesNotMatch(css, /\.bb-app__screen\s*\{[^}]*position:\s*absolute/s)
})

test('screens are laid out up front, but only after the boot crossfade', () => {
  const app = source('App.tsx')
  const css = source('App.css')

  // What actually fixes "the Info screen does not animate on its first
  // visit": a display:none screen has no layout at all, so the first
  // switch to one pays for its whole subtree (66ms for Info, 76ms for
  // Settings, against 15ms on every visit after - see App.tsx) on exactly
  // the frames the entrance animation should be drawing. Warming them at
  // boot moves that cost off the animation.
  assert.match(css, /\[data-prewarm='true'\][^{]*\{[^}]*display:\s*block/s)
  // ...but on a timer, not in the same tick as the crossfade: forcing
  // three screens through layout synchronously while #bb-boot is trying to
  // draw its own first frames turned that fade into a hard cut.
  assert.match(app, /setTimeout\(\s*\(\)\s*=>\s*\{[^}]*setPrewarming\(true\)/)
})

test('the Google Forms document fits its SVG viewBox', () => {
  const icons = source('components/icons.tsx')
  const form = icons.slice(
    icons.indexOf('export function IconFormDocument'),
    icons.indexOf('/** Every icon a link'),
  )

  assert.match(form, /viewBox="0 0 24 24"/)
})

test('the home toggle row reserves the full address height', () => {
  assert.match(source('screens/HomeScreen.css'), /\.bb-home__toggle-row\s*\{[^}]*height:\s*50px/s)
})

test('the rewrite disclosure chevron sits on the right', () => {
  const css = source('screens/SettingsScreen.css')
  assert.match(css, /grid-template-columns:\s*minmax\(0, 1fr\) 18px/)
  assert.match(css, /\.bb-subheading__summary::before\s*\{[^}]*grid-column:\s*2/s)
})
