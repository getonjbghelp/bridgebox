import type { ReactNode } from 'react'
import './Segmented.css'

export interface SegmentedOption<T extends string> {
  value: T
  // A flag emoji for a language option needs ReactNode, not string - every
  // other caller passes plain text, which is still a valid ReactNode.
  label: ReactNode
  // Only needed when `label` isn't itself readable text (a flag emoji) - a
  // screen reader's own name for a flag glyph isn't guaranteed, so the
  // button needs an explicit one rather than relying on it.
  ariaLabel?: string
}

interface SegmentedProps<T extends string> {
  value: T
  options: SegmentedOption<T>[]
  onChange: (value: T) => void
  disabled?: boolean
  ariaLabel?: string
}

/**
 * A choice between two or more peers - not an on/off switch.
 *
 * Deliberately not `Toggle`: a profile's kind is Ecast OR Blobcast, neither of
 * which is the "off" state, and a checkbox-shaped control would imply one of
 * them is a deviation from the other. Both options stay visible and labelled,
 * so the current value reads without interacting.
 */
export function Segmented<T extends string>({
  value,
  options,
  onChange,
  disabled = false,
  ariaLabel,
}: SegmentedProps<T>) {
  return (
    <div className="bb-segmented" role="group" aria-label={ariaLabel}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className="bb-segmented__item"
          aria-pressed={value === option.value}
          aria-label={option.ariaLabel}
          disabled={disabled}
          onClick={() => value !== option.value && onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
