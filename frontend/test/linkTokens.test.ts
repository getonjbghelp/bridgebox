// Self-check for the link-splitting regex behind lib/richText.tsx's [label](url)
// support. Run it with:
//
//   node --test test/linkTokens.test.ts        (from frontend/)

import assert from 'node:assert/strict'
import test from 'node:test'

import { splitLinks } from '../src/lib/linkTokens.ts'

test('plain text with no link is a single token with no url', () => {
  assert.deepEqual(splitLinks('just text'), [{ text: 'just text' }])
})

test('a bare link becomes one token with both text and url', () => {
  assert.deepEqual(splitLinks('[Creative Commons](https://creativecommons.org/)'), [
    { text: 'Creative Commons', url: 'https://creativecommons.org/' },
  ])
})

test('text before and after a link stays split around it', () => {
  assert.deepEqual(splitLinks('See [the site](https://example.com/) for details'), [
    { text: 'See ' },
    { text: 'the site', url: 'https://example.com/' },
    { text: ' for details' },
  ])
})

test('two links in one string both come through', () => {
  const tokens = splitLinks('[a](https://a.example/) and [b](https://b.example/)')
  assert.deepEqual(tokens, [
    { text: 'a', url: 'https://a.example/' },
    { text: ' and ' },
    { text: 'b', url: 'https://b.example/' },
  ])
})

test('a non-http(s) scheme is left as literal text, not a link', () => {
  // javascript: URLs and the like should never come out clickable, even
  // though this text is authored rather than user input - see the module
  // docstring.
  assert.deepEqual(splitLinks('[click me](javascript:alert(1))'), [
    { text: '[click me](javascript:alert(1))' },
  ])
})

test('an empty string is one empty token, not zero tokens', () => {
  assert.deepEqual(splitLinks(''), [{ text: '' }])
})
