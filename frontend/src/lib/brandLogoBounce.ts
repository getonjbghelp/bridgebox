/**
 * Timing for the letter-bounce easter egg (click the expanded sidebar
 * wordmark - see Sidebar.tsx and BrandLogo.css). Kept out of BrandLogo.tsx,
 * which is JSX from top to bottom, specifically so node --test can import it:
 * this project's test runner cannot load a .tsx file at all (no JSX
 * transform), the same reason lib/releaseNotes.ts and lib/changelog.ts exist
 * as plain .ts modules next to the components that use them.
 */

/** Per-letter start stagger. */
export const BOUNCE_STEP_MS = 55
/** One letter's own hop, start to settled. */
export const BOUNCE_DURATION_MS = 420
// b-r-i-d-g-e-b-o-x: 9 letters (the i-tittle rides with "i" at the same
// delay, not its own step - seven distinct glyphs plus the two B's).
const BOUNCE_STEPS = 9
/** The whole wave, first letter's start to last letter's settle - what
 * Sidebar.tsx times its auto-stop timeout against, so the two can never
 * drift out of sync with each other. */
export const BOUNCE_TOTAL_MS = BOUNCE_DURATION_MS + (BOUNCE_STEPS - 1) * BOUNCE_STEP_MS

/** Delay for the Nth letter in the b-r-i-d-g-e-b-o-x sequence (0-indexed),
 * as a CSS custom property BrandLogo.css's animation-delay reads. */
export function bounceHop(step: number): Record<string, string> {
  return { '--bb-bounce-delay': `${step * BOUNCE_STEP_MS}ms` }
}
