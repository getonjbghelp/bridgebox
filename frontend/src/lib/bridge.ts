// Thin wrapper around the pywebview JS API bridge (window.pywebview.api,
// registered by backend/bridgebox/desktop.py's Api class). In plain browser
// dev mode (no pywebview host) calls fail loudly instead of silently
// hanging, so screens can fall back to placeholder data.

declare global {
  interface Window {
    pywebview?: {
      api?: Record<string, (...args: unknown[]) => Promise<unknown>>
    }
  }
}

const BRIDGE_READY_TIMEOUT_MS = 3000
const POLL_INTERVAL_MS = 50

export function isNativeBridgeAvailable(): boolean {
  return typeof window !== 'undefined' && !!window.pywebview?.api
}

// ponytail: 50ms poll instead of subscribing to 'pywebviewready'. The event
// is one-shot and may already have fired before this module ran, so polling
// the thing we actually need is both shorter and harder to get wrong.
// Upgrade path if this interval ever shows up as real overhead (e.g. in the
// motion tracer, or BRIDGE_READY_TIMEOUT_MS needing to grow for a slow
// machine): attach the 'pywebviewready' listener AND check the predicate
// synchronously right after attaching it - that closes the race without
// polling, at the cost of two code paths instead of one.
function pollUntil(predicate: () => boolean, timeoutMs: number): Promise<boolean> {
  if (predicate()) return Promise.resolve(true)
  if (typeof window === 'undefined') return Promise.resolve(false)
  return new Promise((resolve) => {
    const deadline = Date.now() + timeoutMs
    const id = window.setInterval(() => {
      if (predicate()) {
        window.clearInterval(id)
        resolve(true)
      } else if (Date.now() > deadline) {
        window.clearInterval(id)
        resolve(false)
      }
    }, POLL_INTERVAL_MS)
  })
}

// Wait for window.pywebview.api to exist at all. Screens use this to decide
// whether to attempt bridge calls on mount; it is NOT proof any given method
// is callable yet - see callBridge.
export function waitForBridgeReady(timeoutMs = BRIDGE_READY_TIMEOUT_MS): Promise<void> {
  return pollUntil(isNativeBridgeAvailable, timeoutMs).then(() => undefined)
}

function hasBridgeMethod(method: string): boolean {
  return typeof window !== 'undefined' && typeof window.pywebview?.api?.[method] === 'function'
}

export async function callBridge<T>(method: string, ...args: unknown[]): Promise<T> {
  // pywebview creates window.pywebview.api and populates its methods as two
  // separate steps, so an `api` object that already exists is no guarantee
  // the method is attached. A mount-time effect (get_config, bridge_status)
  // routinely lands in that gap and used to fail permanently with "bridge
  // method not implemented" - hence waiting on the method itself, not on the
  // object holding it.
  if (!hasBridgeMethod(method) && !(await pollUntil(() => hasBridgeMethod(method), BRIDGE_READY_TIMEOUT_MS))) {
    if (!isNativeBridgeAvailable()) {
      throw new Error(`pywebview bridge unavailable (dev mode) - method: ${method}`)
    }
    throw new Error(`bridge method not implemented: ${method}`)
  }
  return window.pywebview!.api![method](...args) as Promise<T>
}

// A callBridge() promise rejecting (bridge unreachable, method not
// implemented) is not something the non-technical, Russian-primary audience
// this app is built for should ever see verbatim - screens show
// strings.common.unexpectedError instead and call this so the real cause
// isn't lost, just moved to devtools where it belongs.
export function logBridgeError(err: unknown): void {
  console.error('[bridge]', err)
}
