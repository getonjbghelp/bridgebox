import type { Transition } from 'framer-motion'
import { useMotionPrefs } from '../state/MotionPrefsContext'

// Every transition below is a fixed ratio against this one duration - not an
// independent constant - so UiConfig.animation_duration_ms's dial (Settings'
// "Длительность анимаций") scales the app's whole motion language together
// instead of retuning each hook by hand. The ratios themselves are what used
// to be these hooks' hardcoded seconds, kept as ratios of the old 220ms
// default so today's feel is exactly reproduced at that default.
const BASE_DURATION_S = 0.22

const INSTANT: Transition = { duration: 0 }

function useDurationScale(): number {
  const { animationDurationMs } = useMotionPrefs()
  return animationDurationMs / 1000 / BASE_DURATION_S
}

// bounce/duration maps closely to Apple's damping-ratio/response pair
// (see apple-design skill). A flat bounce: 0 (critically damped) reads as
// mechanical rather than fluid for a surface this size appearing - a little
// overshoot is what makes the settle feel alive instead of just "arrived".
export function useSpringTransition(): Transition {
  const { animationsEnabled } = useMotionPrefs()
  const scale = useDurationScale()
  return animationsEnabled ? { type: 'spring', bounce: 0.14, duration: 0.45 * scale } : INSTANT
}

// For a small control's own geometry moving (a toggle thumb's ~20px slide, the
// sidebar's active-item pill) rather than a surface entering the screen.
// SPRING_DEFAULT's duration is tuned for something a whole modal's size, so
// reusing it here made a two-frame flip look like it was dragging its feet -
// the physical distance is an order of magnitude smaller, so the settle time
// should be too. Same bounce as SPRING_DEFAULT for one consistent "material",
// just faster.
export function useMicroTransition(): Transition {
  const { animationsEnabled } = useMotionPrefs()
  const scale = useDurationScale()
  return animationsEnabled ? { type: 'spring', bounce: 0.14, duration: 0.22 * scale } : INSTANT
}

// Short tweens rather than a spring, for a swap under AnimatePresence
// mode="wait": there the outgoing element's exit has to finish before the
// incoming one mounts, so the two durations add up. At a 0.45s-class spring
// that would be most of a second of nothing between steps - long enough to
// read as the app having hung on a click. Shared by the setup wizard's own
// step transitions AND the Settings tab switch - one swap speed for the
// whole app, not a special faster one for tabs.
export function useStepTransition(): { out: Transition; in: Transition } {
  const { animationsEnabled } = useMotionPrefs()
  const scale = useDurationScale()
  if (!animationsEnabled) return { out: INSTANT, in: INSTANT }
  return {
    out: { duration: 0.13 * scale, ease: [0.4, 0, 1, 1] },
    in: { duration: 0.22 * scale, ease: [0.2, 0.8, 0.2, 1] },
  }
}
