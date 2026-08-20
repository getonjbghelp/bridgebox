import { motion } from 'framer-motion'
import { useSpringTransition } from '../lib/motion'
import './Toggle.css'

interface ToggleProps {
  checked: boolean
  onChange: (checked: boolean) => void
  size?: 'sm' | 'lg'
  label?: string
  disabled?: boolean
}

export function Toggle({ checked, onChange, size = 'sm', label, disabled }: ToggleProps) {
  const transition = useSpringTransition()

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
