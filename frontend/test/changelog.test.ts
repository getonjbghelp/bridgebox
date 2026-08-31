// Self-check for the GitHub-release changelog parser behind BetaBadge.
// Run it with:
//
//   node --test test/changelog.test.ts        (from frontend/)

import assert from 'node:assert/strict'
import test from 'node:test'

import { extractMarker, isAtLeast, toChangelogEntry, type GithubRelease } from '../src/lib/changelog.ts'

// ---- extractMarker ---------------------------------------------------------

test('the guillemet marker is split into title, level, and the remaining body', () => {
  const result = extractMarker('«Быстрый прогрев» • MINOR\n\nТело записи.\nВторая строка.')
  assert.equal(result.title, 'Быстрый прогрев')
  assert.equal(result.level, 'minor')
  assert.equal(result.body, 'Тело записи.\nВторая строка.')
})

test('straight double quotes work the same as guillemets', () => {
  const result = extractMarker('"Faster warm-up" • MAJOR\n\nBody text.')
  assert.equal(result.title, 'Faster warm-up')
  assert.equal(result.level, 'major')
})

test('curly quotes work too', () => {
  const result = extractMarker('“Faster warm-up” • CRITICAL\n\nBody text.')
  assert.equal(result.title, 'Faster warm-up')
  assert.equal(result.level, 'critical')
})

test('single quotes work too', () => {
  const result = extractMarker("'Faster warm-up' • minor\n\nBody text.")
  assert.equal(result.title, 'Faster warm-up')
  assert.equal(result.level, 'minor')
})

test('the level word is case-insensitive', () => {
  assert.equal(extractMarker('«X» • Minor').level, 'minor')
  assert.equal(extractMarker('«X» • CRITICAL').level, 'critical')
})

test('blank lines before the marker do not stop it from being found', () => {
  const result = extractMarker('\n\n  «Прогрев» • MINOR\n\nТело.')
  assert.equal(result.title, 'Прогрев')
  assert.equal(result.body, 'Тело.')
})

test('a release with no marker at all falls back to no title and the whole text as body', () => {
  const result = extractMarker('Just a plain release body with no marker line.')
  assert.equal(result.title, null)
  assert.equal(result.level, null)
  assert.equal(result.body, 'Just a plain release body with no marker line.')
})

test('mismatched quote pairs do not confuse the parser into misreading the title', () => {
  // «Title" - opens with a guillemet, closes with a straight quote. Still a
  // real attempt at the convention, so it should still parse rather than
  // silently falling through to "no title at all".
  const result = extractMarker('«Мисматч" • MINOR\n\nТело.')
  assert.equal(result.title, 'Мисматч')
  assert.equal(result.level, 'minor')
})

// ---- isAtLeast --------------------------------------------------------------

test('a version below the cutoff is not at least it', () => {
  assert.equal(isAtLeast('0.1.5', [0, 1, 6]), false)
})

test('a version exactly at the cutoff counts as at least it', () => {
  assert.equal(isAtLeast('0.1.6', [0, 1, 6]), true)
})

test('a version above the cutoff counts as at least it', () => {
  assert.equal(isAtLeast('0.2.0', [0, 1, 6]), true)
  assert.equal(isAtLeast('0.1.10', [0, 1, 6]), true)
})

test('a leading v is stripped before comparing', () => {
  assert.equal(isAtLeast('v0.1.7', [0, 1, 6]), true)
})

test('a pre-release suffix does not break the comparison', () => {
  assert.equal(isAtLeast('0.1.6b1', [0, 1, 6]), true)
  assert.equal(isAtLeast('0.1.5b3', [0, 1, 6]), false)
})

test('an unparseable version is never "at least" anything', () => {
  assert.equal(isAtLeast('not-a-version', [0, 1, 6]), false)
})

// ---- toChangelogEntry -------------------------------------------------------

function release(overrides: Partial<GithubRelease> = {}): GithubRelease {
  return {
    version: '0.1.6',
    name: '0.1.6',
    body: '',
    date: '2026-09-01',
    htmlUrl: 'https://github.com/getonjbghelp/bridgebox/releases/tag/v0.1.6',
    ...overrides,
  }
}

test('a properly formatted bilingual release parses both languages', () => {
  const body = [
    '<details>',
    '<summary>English Version</summary>',
    '',
    '«Faster warm-up» • MINOR',
    '',
    'The bridge connects faster now.',
    '</details>',
    '',
    '«Быстрый прогрев» • MINOR',
    '',
    'Мост теперь подключается быстрее.',
  ].join('\n')

  const entry = toChangelogEntry(release({ body }))

  assert.equal(entry.version, '0.1.6')
  assert.equal(entry.level, 'minor')
  assert.equal(entry.ru.title, 'Быстрый прогрев')
  assert.equal(entry.ru.body, 'Мост теперь подключается быстрее.')
  assert.equal(entry.en.title, 'Faster warm-up')
  assert.equal(entry.en.body, 'The bridge connects faster now.')
})

test('a release with no marker falls back to the release name and level minor', () => {
  const entry = toChangelogEntry(release({ name: '0.1.6 - hotfix', body: 'Fixed a crash.' }))

  assert.equal(entry.level, 'minor')
  assert.equal(entry.ru.title, '0.1.6 - hotfix')
  assert.equal(entry.en.title, '0.1.6 - hotfix')
})

test('only one language following the convention still gets its level applied to both', () => {
  const body = [
    '<details>',
    '<summary>English Version</summary>',
    '',
    'No marker here, just prose.',
    '</details>',
    '',
    '«С заголовком» • CRITICAL',
    '',
    'Тело.',
  ].join('\n')

  const entry = toChangelogEntry(release({ name: '0.1.6 (release name)', body }))

  assert.equal(entry.level, 'critical')
  assert.equal(entry.ru.title, 'С заголовком')
  // EN never found its own marker, so it falls back to the release's name -
  // not to RU's title, which would be a language mixup hiding as a fallback.
  assert.equal(entry.en.title, '0.1.6 (release name)')
})
