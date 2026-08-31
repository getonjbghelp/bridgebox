import assert from 'node:assert/strict'
import test from 'node:test'
import { svgAccent } from '../src/lib/svgAccent.ts'

test('svgAccent picks the first visible brand paint from an SVG', () => {
  assert.equal(svgAccent('<svg><path fill="#fff"/><path fill="#50af95"/></svg>'), '#50af95')
})
test('svgAccent reads gradient stop colours when no solid fill is present', () => {
  assert.equal(svgAccent('<svg><stop stop-color="#2AABEE"/></svg>'), '#2AABEE')
})
test('svgAccent ignores non-colours and returns no accent when absent', () => {
  assert.equal(svgAccent('<svg><path fill="currentColor"/></svg>'), null)
})
