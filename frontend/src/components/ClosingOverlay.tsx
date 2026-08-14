import { useEffect, useState } from 'react'
import { useStrings } from '../lib/strings'
import './ClosingOverlay.css'

/**
 * The "we are shutting down, please wait" screen.
 *
 * Not a `Modal`: this one must never be dismissable. No close button, no
 * Escape, no click-outside - the app is already tearing down its bridge,
 * killing winws and closing sockets, and there is nothing left to go back to.
 * It disappears when the window does.
 *
 * Driven by a plain DOM event rather than a bridge call, because the backend
 * is the one that knows when a close was accepted (see desktop.py's
 * `_begin_shutdown`). The event costs nothing if nobody is listening, so a
 * frontend that crashed earlier simply shuts down without the animation
 * instead of blocking the shutdown on its own health.
 */
export const CLOSING_EVENT = 'bb:closing'

export function ClosingOverlay() {
  const strings = useStrings()
  const [closing, setClosing] = useState(false)

  useEffect(() => {
    const onClosing = () => setClosing(true)
    window.addEventListener(CLOSING_EVENT, onClosing)
    return () => window.removeEventListener(CLOSING_EVENT, onClosing)
  }, [])

  if (!closing) return null

  return (
    <div className="bb-closing" role="alertdialog" aria-live="assertive" aria-busy="true">
      <div className="bb-closing__panel">
        <h2 className="bb-closing__title">{strings.common.closingTitle}</h2>
        <p className="bb-closing__body">{strings.common.closingBody}</p>
        {/*
          An indeterminate bar rather than the Spinner: shutdown has no
          progress to report - it takes as long as taskkill and the socket
          teardown take - and a sliding bar says "still working" without
          implying a percentage nobody can compute.
        */}
        <div className="bb-closing__track">
          <div className="bb-closing__slider" />
        </div>
      </div>
    </div>
  )
}
