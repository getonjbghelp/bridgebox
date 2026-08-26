// Self-check for the bilingual release-body splitter behind the update modal.
// Run it with:
//
//   node --test test/releaseNotes.test.ts        (from frontend/)

import assert from 'node:assert/strict'
import test from 'node:test'

import { pickReleaseNotes, splitReleaseNotes } from '../src/lib/releaseNotes.ts'

// The real body of release 0.1.3 (b1), trimmed to its shape: English folded
// into <details>, Russian left as plain body text after it.
const REAL = `<details>
<summary>English Version</summary>

The most significant change in this version is the **introduction of automatic paste**.

⚠️ Please note: The project is currently in beta.
</details>

Самым крупным изменением в этой версии является появление **автоматической вставки**.

⚠️ Обратите внимание: проект находится на этапе беты.`

test('a Russian reader gets the Russian half and none of the English', () => {
  const notes = pickReleaseNotes(REAL, 'ru')
  assert.ok(notes.includes('автоматической вставки'))
  assert.ok(!notes.includes('automatic paste'))
})

test('an English reader gets the English half and none of the Russian', () => {
  const notes = pickReleaseNotes(REAL, 'en')
  assert.ok(notes.includes('automatic paste'))
  assert.ok(!notes.includes('автоматической вставки'))
})

test('the details/summary tags never reach the renderer', () => {
  for (const locale of ['ru', 'en'] as const) {
    const notes = pickReleaseNotes(REAL, locale)
    assert.ok(!notes.includes('<details'), `${locale} leaked <details>`)
    assert.ok(!notes.includes('</details>'), `${locale} leaked </details>`)
    assert.ok(!notes.includes('<summary'), `${locale} leaked <summary>`)
    assert.ok(!notes.includes('English Version'), `${locale} leaked the summary label`)
  }
})

test('a release with no language markers is shown in full to everyone', () => {
  const plain = 'Just one language here.\n\nSecond paragraph.'
  assert.equal(pickReleaseNotes(plain, 'ru'), plain)
  assert.equal(pickReleaseNotes(plain, 'en'), plain)
})

test('an unlabelled <details> is unwrapped rather than printed as tags', () => {
  const body = '<details>\n<summary>Extra detail</summary>\n\nHidden prose.\n</details>'
  const notes = pickReleaseNotes(body, 'ru')
  assert.ok(notes.includes('Hidden prose.'))
  assert.ok(notes.includes('Extra detail') === false)
  assert.ok(!notes.includes('<details'))
})

test('the convention can flip: folding Russian instead still resolves both ways', () => {
  const flipped = `<details>
<summary>Русская версия</summary>

Русский текст.
</details>

English text.`
  assert.equal(pickReleaseNotes(flipped, 'ru'), 'Русский текст.')
  assert.equal(pickReleaseNotes(flipped, 'en'), 'English text.')
})

test('both languages folded separately still resolve', () => {
  const both = `<details><summary>English Version</summary>

English text.
</details>
<details><summary>Русская версия</summary>

Русский текст.
</details>`
  assert.equal(pickReleaseNotes(both, 'en'), 'English text.')
  assert.equal(pickReleaseNotes(both, 'ru'), 'Русский текст.')
})

test('only the other language present is shown rather than an empty modal', () => {
  const onlyEnglish = '<details><summary>English Version</summary>\n\nEnglish only.\n</details>'
  assert.equal(pickReleaseNotes(onlyEnglish, 'ru'), 'English only.')
})

test('split reports the labelled block and the untagged remainder in order', () => {
  const sections = splitReleaseNotes(REAL)
  assert.equal(sections.length, 2)
  assert.equal(sections[0].locale, 'en')
  assert.equal(sections[1].locale, null)
  assert.ok(sections[1].text.startsWith('Самым крупным'))
})
