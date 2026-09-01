import { useEffect, useState } from 'react'
import { useStrings } from '../lib/strings'
import { callBridge, isNativeBridgeAvailable, waitForBridgeReady } from '../lib/bridge'
import './ConnectionHealthBanner.css'

/**
 * "The bypass seems to have stopped working."
 *
 * Backed by RuntimeCore._health_check_loop - a background re-probe of the
 * game's own servers, running every couple of minutes while the bridge is
 * up (see backend/bridgebox/runtime_core.py). Unlike IntegrityBanner this
 * condition self-heals: no dismiss button, because "hidden until the next
 * launch" would silence a real, later outage just as easily as the blip
 * that prompted the click. The banner disappears on its own the moment a
 * later round succeeds again.
 *
 * Off entirely when Settings → Сеть и обход → "Следить за соединением" is
 * off (health_check.enabled) - the backend then never reports anything but
 * `ok: true`, so this component has nothing to show.
 */
interface HealthStatus {
  ok: boolean
  error: string | null
}

// The backend only updates its answer once every couple of minutes (see
// HEALTH_CHECK_INTERVAL_S) - this just decouples how quickly a flip reaches
// the screen from that cadence, not a tight poll.
const POLL_MS = 20000

export function ConnectionHealthBanner() {
  const strings = useStrings()
  const [status, setStatus] = useState<HealthStatus | null>(null)

  useEffect(() => {
    let cancelled = false
    let id: number | undefined

    async function poll() {
      if (cancelled || !isNativeBridgeAvailable()) return
      try {
        const result = await callBridge<HealthStatus>('connection_health_status')
        if (!cancelled) setStatus(result)
      } catch {
        // Leave the last known status alone - a single failed bridge call is
        // not itself evidence the connection is unhealthy.
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

  if (!status || status.ok) return null

  return (
    <div className="bb-conn-health" role="alert">
      <div className="bb-conn-health__text">
        <strong className="bb-conn-health__title">{strings.common.connHealthTitle}</strong>
        <span className="bb-conn-health__body">{strings.common.connHealthBody}</span>
      </div>
    </div>
  )
}
