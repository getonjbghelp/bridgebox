import type { Transition } from 'framer-motion'
import { useMotionPrefs } from '../state/MotionPrefsContext'

// bounce/duration maps closely to Apple's damping-ratio/response pair
// (see apple-design skill). A flat bounce: 0 (critically damped) reads as
// mechanical rather than fluid for a surface this size appearing - a little
// overshoot is what makes the settle feel alive instead of just "arrived".
const SPRING_DEFAULT: Transition = { type: 'spring', bounce: 0.14, duration: 0.45 }

// For a small control's own geometry moving (a toggle thumb's ~20px slide, the
// sidebar's active-item pill) rather than a surface entering the screen.
// SPRING_DEFAULT's duration is tuned for something a whole modal's size, so
// reusing it here made a two-frame flip look like it was dragging its feet -
// the physical distance is an order of magnitude smaller, so the settle time
// should be too. Same bounce as SPRING_DEFAULT for one consistent "material",
// just faster.
const SPRING_MICRO: Transition = { type: 'spring', bounce: 0.14, duration: 0.22 }

const INSTANT: Transition = { duration: 0 }

export function useSpringTransition(): Transition {
  const { animationsEnabled } = useMotionPrefs()
  return animationsEnabled ? SPRING_DEFAULT : INSTANT
}

export function useMicroTransition(): Transition {
  const { animationsEnabled } = useMotionPrefs()
  return animationsEnabled ? SPRING_MICRO : INSTANT
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
