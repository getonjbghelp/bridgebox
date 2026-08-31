// The one piece of the letter-bounce easter egg worth a standalone test:
// BOUNCE_TOTAL_MS is imported by Sidebar.tsx to time the auto-stop timeout,
// while BOUNCE_DURATION_MS/BOUNCE_STEP_MS drive the actual CSS animation in
// BrandLogo.css - a change to either number that forgot to also move
// BOUNCE_TOTAL_MS would silently desync "when the animation really ends" from
// "when Sidebar thinks it ended", which is exactly the kind of drift a quick
// arithmetic pin catches for free.
import assert from 'node:assert/strict'
import test from 'node:test'

import { BOUNCE_DURATION_MS, BOUNCE_STEP_MS, BOUNCE_TOTAL_MS, bounceHop } from '../src/lib/brandLogoBounce.ts'

test('BOUNCE_TOTAL_MS covers one full hop plus every letter after the first starting late', () => {
  // b-r-i-d-g-e-b-o-x: 9 letters, i-tittle rides with "i" at the same delay
  // rather than adding a 10th step - see brandLogoBounce.ts's own comment.
  const letterCount = 9
  assert.equal(BOUNCE_TOTAL_MS, BOUNCE_DURATION_MS + (letterCount - 1) * BOUNCE_STEP_MS)
})

test('bounceHop scales the delay by the step, in milliseconds', () => {
  assert.deepEqual(bounceHop(0), { '--bb-bounce-delay': '0ms' })
  assert.deepEqual(bounceHop(3), { '--bb-bounce-delay': `${3 * BOUNCE_STEP_MS}ms` })
})
