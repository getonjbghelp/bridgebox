import { motion } from 'framer-motion'
import { useMicroTransition } from '../lib/motion'
import './Toggle.css'

interface ToggleProps {
  checked: boolean
  onChange: (checked: boolean) => void
  size?: 'sm' | 'lg'
  // Required, not optional: this is a role="switch" with no visible text of
  // its own, so a missing label is a silent screen-reader dead end, not a
  // cosmetic gap - "switch, not checked" with nothing saying which setting.
  // Making it required means a new Toggle without one fails the build
  // instead of shipping unlabeled.
  label: string
  disabled?: boolean
}

export function Toggle({ checked, onChange, size = 'sm', label, disabled }: ToggleProps) {
  const transition = useMicroTransition()

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className={`bb-toggle bb-toggle--${size}${checked ? ' bb-toggle--on' : ''}`}
      onClick={() => !disabled && onChange(!checked)}
    >
      <motion.span className="bb-toggle__thumb" layout transition={transition} />
    </button>
  )
}
