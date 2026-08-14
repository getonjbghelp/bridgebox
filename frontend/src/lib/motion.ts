import type { Transition } from 'framer-motion'
import { useMotionPrefs } from '../state/MotionPrefsContext'

// bounce/duration maps closely to Apple's damping-ratio/response pair
// (see apple-design skill). bounce: 0 == damping 1.0 == critically damped,
// used for structural UI change by default.
const SPRING_DEFAULT: Transition = { type: 'spring', bounce: 0, duration: 0.5 }

// A little bounce (~damping 0.8), reserved for momentum-driven interactions
// (drag release, scroll snap) - never for plain fades/mounts.
const SPRING_MOMENTUM: Transition = { type: 'spring', bounce: 0.18, duration: 0.55 }

const INSTANT: Transition = { duration: 0 }

export function useSpringTransition(kind: 'default' | 'momentum' = 'default'): Transition {
  const { animationsEnabled } = useMotionPrefs()
  if (!animationsEnabled) return INSTANT
  return kind === 'momentum' ? SPRING_MOMENTUM : SPRING_DEFAULT
}

// Short tweens rather than SPRING_DEFAULT, for a swap under AnimatePresence
// mode="wait": there the outgoing element's exit has to finish before the
// incoming one mounts, so the two durations add up. At the 0.5s spring that
// is a full second of nothing between wizard steps - long enough to read as
// the app having hung on a button press.
const STEP_OUT: Transition = { duration: 0.13, ease: [0.4, 0, 1, 1] }
const STEP_IN: Transition = { duration: 0.22, ease: [0.2, 0.8, 0.2, 1] }

export function useStepTransition(): { out: Transition; in: Transition } {
  const { animationsEnabled } = useMotionPrefs()
  if (!animationsEnabled) return { out: INSTANT, in: INSTANT }
  return { out: STEP_OUT, in: STEP_IN }
}
