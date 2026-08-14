import { motion } from 'framer-motion'
import { useStrings } from '../lib/strings'
import { IconCheck, IconClose } from './icons'
import { Spinner } from './Spinner'
import './DiagBadge.css'

export type DiagState = 'idle' | 'running' | 'done' | 'error'

/**
 * Shared by the connection test on Запуск and the strategy test in Settings.
 *
 * Uses icons rather than the "OK"/"✕" text it used to: U+2715 exists in
 * neither Inter nor Manrope, so that glyph was silently falling back to
 * whatever system font had it (see assets/fonts/README.md).
 */
export function DiagBadge({ state }: { state: DiagState }) {
  const strings = useStrings()
  if (state === 'idle') return null

  if (state === 'running') return <Spinner size={16} label={strings.common.checkingLabel} />

  const ok = state === 'done'
  return (
    <motion.span
      className={`bb-diag-badge bb-diag-badge--${ok ? 'ok' : 'error'}`}
      initial={{ opacity: 0, scale: 0.8 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.15 }}
      role="status"
      aria-label={ok ? strings.common.successAriaLabel : strings.common.errorAriaLabel}
    >
      {ok ? <IconCheck size={13} /> : <IconClose size={13} />}
    </motion.span>
  )
}
