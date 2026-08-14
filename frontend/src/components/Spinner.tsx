import { useStrings } from '../lib/strings'
import './Spinner.css'

/**
 * CSS keyframes rather than a framer loop, deliberately: a CSS animation dies
 * with its node, while an infinite framer animation has to be torn down by
 * hand and leaks if a component unmounts mid-wait.
 *
 * Only for waits that are genuinely long - bridge start (cert generation, CA
 * install, winws launch), the connection test, the strategy suite. Anything
 * under ~300ms flashes and reads worse than no indicator at all.
 */
export function Spinner({ size = 16, label }: { size?: number; label?: string }) {
  const strings = useStrings()
  return (
    <span
      className="bb-spinner"
      style={{ width: size, height: size }}
      role="status"
      aria-label={label ?? strings.common.loadingAriaLabel}
    >
      <svg viewBox="0 0 20 20" width={size} height={size} aria-hidden="true">
        <circle
          cx="10"
          cy="10"
          r="8"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          opacity="0.18"
        />
        <path
          d="M10 2a8 8 0 0 1 8 8"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.2"
          strokeLinecap="round"
        />
      </svg>
    </span>
  )
}
