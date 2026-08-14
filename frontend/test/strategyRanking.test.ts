// Self-check for the setup wizard's strategy ranking. Run it with:
//
//   node --test test/strategyRanking.test.ts        (from frontend/)
//
// What this guards: the wizard runs the suite with target_set="both", which
// makes the backend report every strategy TWICE - once per protocol. Ranking
// that list without collapsing the pairs would offer the same strategy twice
// and could recommend one that only half works.

import assert from 'node:assert/strict'
import test from 'node:test'

import { aggregateResults, type StrategyResult } from '../src/lib/strategyRanking.ts'

function row(
  key: string,
  targetSet: 'ecast' | 'blobcast',
  ms: number | null,
  ok = ms !== null,
): StrategyResult {
  return {
    key,
    name: key.toUpperCase(),
    ok,
    error: ok ? null : 'timeout',
    targetSet,
    targets: {
      [`${targetSet}.jackboxgames.com`]: {
        ok: ms !== null,
        elapsedMs: ms,
        status: ms === null ? null : 200,
        error: null,
      },
    },
  }
}

test('collapses a both-protocol run to one row per strategy', () => {
  const ranked = aggregateResults([
    row('general', 'ecast', 120),
    row('general', 'blobcast', 180),
  ])

  assert.equal(ranked.length, 1)
  assert.equal(ranked[0].key, 'general')
})

test('times a strategy by its slowest pass, not its fastest', () => {
  // 180 is what the player waits through if their game speaks Blobcast, and
  // the wizard is picking before anyone has said which game that is.
  const [only] = aggregateResults([row('general', 'ecast', 120), row('general', 'blobcast', 180)])

  assert.equal(only.ms, 180)
  assert.equal(only.ok, true)
})

test('a strategy that fails either protocol is not ok', () => {
  const [only] = aggregateResults([
    row('alt3', 'ecast', 90),
    row('alt3', 'blobcast', null, false),
  ])

  assert.equal(only.ok, false)
  assert.equal(only.ms, null)
})

test('working strategies sort ahead of broken ones, then by time', () => {
  const ranked = aggregateResults([
    row('slow', 'ecast', 400),
    row('slow', 'blobcast', 410),
    row('dead', 'ecast', null, false),
    row('dead', 'blobcast', null, false),
    row('fast', 'ecast', 90),
    row('fast', 'blobcast', 110),
  ])

  assert.deepEqual(
    ranked.map((entry) => entry.key),
    ['fast', 'slow', 'dead'],
  )
})

test('a row that reports ok with no responding target is still not ok', () => {
  // The suite marks a strategy ok when winws switched cleanly, which is not
  // the same as any Jackbox host having answered through it - trusting the
  // flag alone would recommend a strategy that measured nothing at all.
  const [only] = aggregateResults([
    {
      key: 'ghost',
      name: 'GHOST',
      ok: true,
      error: null,
      targetSet: 'ecast',
      targets: {},
    },
  ])

  assert.equal(only.ok, false)
  assert.equal(only.ms, null)
})

test('an empty run ranks nothing rather than throwing', () => {
  assert.deepEqual(aggregateResults([]), [])
})
