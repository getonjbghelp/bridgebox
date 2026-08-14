import { useEffect, useState } from 'react'
import { Button } from './Button'
import { useStrings, t } from '../lib/strings'
import { callBridge, isNativeBridgeAvailable, waitForBridgeReady } from '../lib/bridge'
import './IntegrityBanner.css'

/**
 * "The program's files have been modified."
 *
 * Rendered above every screen and above the wizard, because the thing it warns
 * about affects all of them equally: BridgeBox runs its own folder's contents
 * as Administrator, and that folder is writable by any local account.
 *
 * Two ways out, and they mean different things. «Скрыть» closes it for this
 * session - the next launch checks again. «Больше не показывать» writes a flag
 * to config.yaml, for somebody who edits their own strategies on purpose and
 * does not need telling every time.
 *
 * The check itself runs once at startup in the backend, so this polls briefly
 * rather than asking for work: hashing a few hundred files finishes a moment
 * after the window appears, and a banner that arrives late is better than a
 * window that waits for one.
 */
interface IntegrityStatus {
  ok: boolean
  verified: boolean
  dismissed: boolean
  baselineMissing: boolean
  changed: string[]
  missing: string[]
  added: string[]
  total: number
}

const POLL_MS = 1500
const MAX_POLLS = 20

export function IntegrityBanner() {
  const strings = useStrings()
  const [status, setStatus] = useState<IntegrityStatus | null>(null)
  const [hidden, setHidden] = useState(false)

  useEffect(() => {
    let cancelled = false
    let attempts = 0
    let id: number | undefined

    async function poll() {
      if (cancelled || !isNativeBridgeAvailable()) return
      try {
        const result = await callBridge<IntegrityStatus>('integrity_status')
        if (cancelled) return
        setStatus(result)
        // Stop as soon as there is something to say, or once the check has
        // clearly had its chance - this is a one-shot answer, not a feed.
        if (!result.verified || ++attempts >= MAX_POLLS) {
          if (id !== undefined) window.clearInterval(id)
        }
      } catch {
        if (id !== undefined) window.clearInterval(id)
      }
    }

    waitForBridgeReady().then(() => {
      if (cancelled || !isNativeBridgeAvailable()) return
      poll()
      id = window.setInterval(poll, POLL_MS)
    })

    return () => {
      cancelled = true
      if (id !== undefined) window.clearInterval(id)
    }
  }, [])

  async function dismissForever() {
    setHidden(true)
    if (!isNativeBridgeAvailable()) return
    await callBridge('dismiss_integrity_warning').catch(() => {})
  }

  // baselineMissing is not a warning: it is the state of a fresh install for
  // the instant before the baseline is written.
  if (
    hidden ||
    !status ||
    status.verified ||
    status.dismissed ||
    status.baselineMissing
  ) {
    return null
  }

  const examples = [...status.changed, ...status.missing, ...status.added].slice(0, 3)

  return (
    <div className="bb-integrity" role="alert">
      <div className="bb-integrity__text">
        <strong className="bb-integrity__title">{strings.common.integrityTitle}</strong>
        <span className="bb-integrity__body">{strings.common.integrityBody}</span>
        <span className="text-caption bb-integrity__files">
          {t(strings.common.integrityCount, { count: status.total })}
          {examples.length > 0 && <> — <span className="text-mono">{examples.join(', ')}</span></>}
        </span>
      </div>
      <div className="bb-integrity__actions">
        <Button variant="ghost" onClick={() => setHidden(true)}>
          {strings.common.integrityHide}
        </Button>
        <Button variant="secondary" onClick={dismissForever}>
          {strings.common.integrityDismiss}
        </Button>
      </div>
    </div>
  )
}
