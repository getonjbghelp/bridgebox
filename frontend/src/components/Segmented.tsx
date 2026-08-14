import './Segmented.css'

export interface SegmentedOption<T extends string> {
  value: T
  label: string
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
          disabled={disabled}
          onClick={() => value !== option.value && onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}
