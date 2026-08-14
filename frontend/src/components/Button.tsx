import { motion } from 'framer-motion'
import type { ReactNode } from 'react'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import './Button.css'

interface ButtonProps {
  variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
  onClick?: () => void
  disabled?: boolean
  type?: 'button' | 'submit'
  children: ReactNode
  fullWidth?: boolean
  title?: string
  ariaLabel?: string
}

export function Button({
  variant = 'secondary',
  onClick,
  disabled,
  type = 'button',
  children,
  fullWidth,
  title,
  ariaLabel,
}: ButtonProps) {
  // The tap scale used to run unconditionally, so turning animations off in
  // Settings left it going - the one motion in the app the switch missed.
  const { animationsEnabled } = useMotionPrefs()

  return (
    <motion.button
      type={type}
      className={`bb-button bb-button--${variant}${fullWidth ? ' bb-button--full' : ''}`}
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={ariaLabel}
      whileTap={disabled || !animationsEnabled ? undefined : { scale: 0.97 }}
      transition={{ duration: 0.1 }}
    >
      {children}
    </motion.button>
  )
}
