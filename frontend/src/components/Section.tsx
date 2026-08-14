import type { ReactNode } from 'react'
import './Section.css'

export function Section({
  title,
  description,
  children,
}: {
  title: string
  description?: string
  children: ReactNode
}) {
  return (
    <section className="bb-section">
      <div className="bb-section__heading">
        <h2 className="text-title">{title}</h2>
        {description && <p className="text-caption">{description}</p>}
      </div>
      <div className="bb-section__body">{children}</div>
    </section>
  )
}

export function Row({
  label,
  hint,
  control,
}: {
  label: string
  hint?: string
  control: ReactNode
}) {
  return (
    <div className="bb-row">
      <div className="bb-row__text">
        <span className="text-body" style={{ color: 'var(--color-text-primary)' }}>
          {label}
        </span>
        {hint && <span className="text-caption">{hint}</span>}
      </div>
      <div className="bb-row__control">{control}</div>
    </div>
  )
}
