import { useEffect, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { BrandLogo } from '../components/BrandLogo'
import { BetaBadge } from '../components/BetaBadge'
import { Toggle } from '../components/Toggle'
import { Button } from '../components/Button'
import { Modal } from '../components/Modal'
import { ConnectGuide } from '../components/ConnectGuide'
import { DiagBadge, type DiagState } from '../components/DiagBadge'
import { IconCheck, IconCopy } from '../components/icons'
import { useSpringTransition } from '../lib/motion'
import { useStrings, t } from '../lib/strings'
import { callBridge, isNativeBridgeAvailable, logBridgeError, waitForBridgeReady } from '../lib/bridge'
import './HomeScreen.css'

// Used only as a fallback when previewing in a plain browser (no pywebview
// host) - the real host/port come from bridge_status()/get_config().
const FALLBACK_HOST = '127.0.0.1'
const FALLBACK_PORT = 8443

interface BridgeStatus {
  ok: boolean
  error: string | null
  running: boolean
  host: string
  port: number
  zapretRunning: boolean
  zapretPid: number | null
  zapretError: string | null
  certInstalled: boolean
  /** Why the bridge went down without anybody pressing anything - today only
   *  "the winws console was closed by hand". Null the rest of the time. */
  zapretNotice?: string | null
}

interface TestConnectionResponse {
  ok: boolean
  error: string | null
  steps: string[]
}

/** The result of the startup check main() fires when «Проверять при запуске»
 *  is on. Read, never started from here - if the setting is off this reports
 *  `started: false` and the notice simply never appears. */
interface StartupUpdateCheck {
  ok: boolean
  started: boolean
  done: boolean
  installed: string | null
  latest: string | null
  updateAvailable: boolean
}

export function HomeScreen() {
  const strings = useStrings()
  const [status, setStatus] = useState<BridgeStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [bridgeError, setBridgeError] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  const [showInstructions, setShowInstructions] = useState(false)
  const [connectionTest, setConnectionTest] = useState<DiagState>('idle')
  const [connectionSteps, setConnectionSteps] = useState<string[]>([])
  const [connectionError, setConnectionError] = useState<string | null>(null)
  const [zapretUpdate, setZapretUpdate] = useState<StartupUpdateCheck | null>(null)
  const [updateStarting, setUpdateStarting] = useState(false)
  const [updateError, setUpdateError] = useState<string | null>(null)
  const transition = useSpringTransition()

  const enabled = status?.running ?? false
  const host = status?.host ?? FALLBACK_HOST
  const port = status?.port ?? FALLBACK_PORT
  const address = `${host}:${port}`
  // Game prepends the scheme itself - a serverUrl value that already
  // includes "https://" is rejected/ignored (confirmed empirically).
  const launchOption = `-jbg.config serverUrl=${address}`

  // Polled, not read once. Three things can now change the bridge's state
  // without this screen being the one that asked: the tray's "Остановить
  // мост", the zapret watchdog when the winws console is closed by hand, and
  // an update tearing the bridge down. A status read once on mount showed a
  // running bridge long after it had stopped.
  useEffect(() => {
    let cancelled = false
    let id: number | undefined

    async function readStatus() {
      try {
        const result = await callBridge<BridgeStatus>('bridge_status')
        if (!cancelled) setStatus(result)
      } catch (err) {
        logBridgeError(err)
        if (!cancelled) setBridgeError(strings.common.unexpectedError)
      }
    }

    waitForBridgeReady().then(() => {
      if (cancelled || !isNativeBridgeAvailable()) return
      readStatus()
      // 2s: this is a status light, not a data feed, and each tick is one
      // in-process dict read on the pywebview API thread.
      id = window.setInterval(readStatus, 2000)
      callBridge<StartupUpdateCheck>('startup_update_check')
        .then((result) => setZapretUpdate(result))
        .catch(() => {})
    })

    return () => {
      cancelled = true
      if (id !== undefined) window.clearInterval(id)
    }
    // Only re-subscribes on an actual language switch (the string value
    // changes reference only then) - not on every render, since it's a
    // primitive, not the whole strings object.
  }, [strings.common.unexpectedError])

  // Dismissed locally rather than through the bridge: the backend clears it on
  // the next start(), and a second round trip to acknowledge a message would
  // be a bridge call for a piece of UI state.
  const [dismissedNotice, setDismissedNotice] = useState<string | null>(null)
  const zapretNotice =
    status?.zapretNotice && status.zapretNotice !== dismissedNotice
      ? status.zapretNotice
      : null

  // The steps list is a one-time diagnostic reading, not a persistent status -
  // it stays correct only until the next start/stop, and nothing else ever
  // clears it. Left forever, a successful run from five minutes ago still
  // sits on screen looking exactly as current as one from five seconds ago.
  useEffect(() => {
    if (connectionSteps.length === 0) return
    const id = window.setTimeout(() => {
      setConnectionSteps([])
      setConnectionError(null)
    }, 30_000)
    return () => window.clearTimeout(id)
  }, [connectionSteps])

  async function handleToggle(next: boolean) {
    if (!isNativeBridgeAvailable()) {
      // Dev-mode preview in a plain browser - no real bridge to call.
      setStatus({
        ok: true,
        error: null,
        running: next,
        host: FALLBACK_HOST,
        port: FALLBACK_PORT,
        zapretRunning: false,
        zapretPid: null,
        zapretError: null,
        certInstalled: false,
      })
      return
    }

    setBusy(true)
    setBridgeError(null)
    // Results describe the session that just ended; keeping them next to a
    // freshly restarted bridge would be stale by definition.
    setConnectionTest('idle')
    setConnectionSteps([])
    setConnectionError(null)
    try {
      const result = await callBridge<BridgeStatus>(next ? 'bridge_start' : 'bridge_stop')
      setStatus(result)
      if (!result.ok) setBridgeError(result.error)
      else if (result.zapretError)
        setBridgeError(t(strings.home.zapretErrorPrefix, { error: result.zapretError }))
    } catch (err) {
      logBridgeError(err)
      setBridgeError(strings.common.unexpectedError)
    } finally {
      setBusy(false)
    }
  }

  async function runConnectionTest() {
    setConnectionTest('running')
    setConnectionSteps([])
    setConnectionError(null)
    if (!isNativeBridgeAvailable()) {
      window.setTimeout(() => setConnectionTest('done'), 800)
      return
    }
    try {
      const result = await callBridge<TestConnectionResponse>('test_connection')
      setConnectionSteps(result.steps ?? [])
      setConnectionTest(result.ok ? 'done' : 'error')
      setConnectionError(result.ok ? null : result.error)
    } catch (err) {
      logBridgeError(err)
      setConnectionTest('error')
      setConnectionError(strings.common.unexpectedError)
    }
  }

  /**
   * Kick off the zapret update from the launch screen.
   *
   * Deliberately does NOT try to render progress here: the whole progress
   * modal, the strategy report and the restart prompt already live in
   * Settings, and a second copy of that on the home screen would be two
   * places to keep correct. This starts the job, says whether it started, and
   * points at where to watch it.
   */
  async function startZapretUpdate() {
    setUpdateStarting(true)
    setUpdateError(null)
    if (!isNativeBridgeAvailable()) {
      window.setTimeout(() => setUpdateStarting(false), 600)
      return
    }
    try {
      const result = await callBridge<{ ok: boolean; error: string | null }>(
        'start_zapret_update',
      )
      if (!result.ok) {
        setUpdateError(result.error)
        setUpdateStarting(false)
      }
    } catch (err) {
      logBridgeError(err)
      setUpdateError(strings.common.unexpectedError)
      setUpdateStarting(false)
    }
  }

  function copyAddress() {
    navigator.clipboard?.writeText(address)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="bb-home">
      <div className="bb-home__mark">
        <BrandLogo title={strings.sidebar.brandName} />
        {/* Renders nothing once a final version ships - the badge removing
            itself is exactly what its own tooltip promises. */}
        <BetaBadge />
      </div>

      <div className="bb-home__toggle-block">
        {/* The address stays in flow, so the row genuinely grows and the pair
            re-centres together - the toggle slides left as the address comes
            out of it. Anchoring the address absolutely kept the toggle
            perfectly still, which sounds tidier and looks wrong: the assembly
            ends up hanging off to one side of the screen's centre line.
            width/padding are animated rather than transform because the
            toggle has to actually move, and only a layout change does that. */}
        <div className="bb-home__toggle-row">
          <Toggle
            size="lg"
            checked={enabled}
            onChange={handleToggle}
            disabled={busy}
            label={strings.home.toggleLabel}
          />
          <AnimatePresence>
            {enabled && (
              <motion.div
                className="bb-home__address-clip"
                initial={{ opacity: 0, width: 0, marginLeft: 0 }}
                animate={{ opacity: 1, width: 'auto', marginLeft: 12 }}
                exit={{ opacity: 0, width: 0, marginLeft: 0 }}
                transition={transition}
              >
                {/* The padding, border and background live on this inner box,
                    not on the element being animated. framer resolves
                    width:'auto' by measuring the target, and measuring a box
                    whose own padding is still animating gave it a value ~25px
                    short - so the width settled low and then snapped to the
                    real one at the end, jolting the toggle sideways. With the
                    animated element carrying no padding, the measurement is
                    the final width from the first frame. */}
                <div className="bb-home__address">
                  <span className="text-mono">{address}</span>
                  <button
                    className="bb-home__copy"
                    onClick={copyAddress}
                    aria-label={strings.home.copyAddressAriaLabel}
                  >
                  <AnimatePresence mode="wait" initial={false}>
                    {copied ? (
                      <motion.span
                        key="check"
                        initial={{ opacity: 0, scale: 0.7 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 0.7 }}
                        transition={{ duration: 0.15 }}
                        style={{ color: 'var(--color-success)', display: 'flex' }}
                      >
                        <IconCheck />
                      </motion.span>
                    ) : (
                      <motion.span
                        key="copy"
                        initial={{ opacity: 0, scale: 0.7 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 0.7 }}
                        transition={{ duration: 0.15 }}
                        style={{ display: 'flex' }}
                      >
                        <IconCopy />
                      </motion.span>
                    )}
                    </AnimatePresence>
                  </button>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={busy ? 'busy' : enabled ? 'on' : 'off'}
            className={`bb-home__status${enabled ? ' bb-home__status--on' : ''}`}
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={transition}
          >
            {busy
              ? strings.home.statusApplying
              : enabled
                ? strings.home.statusOn
                : strings.home.statusOff}
          </motion.span>
        </AnimatePresence>
        {bridgeError && <span className="bb-home__error">{bridgeError}</span>}
        {/* Nobody pressed anything - the bridge turned itself off because
            winws died. Saying so is the difference between "the app is
            broken" and "the window you closed was load-bearing". */}
        {zapretNotice && (
          <span className="bb-home__notice">
            {zapretNotice}
            <button
              type="button"
              className="bb-home__notice-close"
              aria-label={strings.common.closeAriaLabel}
              onClick={() => setDismissedNotice(zapretNotice)}
            >
              ×
            </button>
          </span>
        )}
      </div>

      {/* Only while the bridge is up: the test creates a real room *through*
          the bridge, so with it stopped it can only ever fail - which is what
          it used to do, confusingly, from the Settings screen. */}
      <AnimatePresence>
        {enabled && (
          <motion.div
            className="bb-home__diag"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            // Follows the address rather than racing it: switching on reads as
            // one gesture - the address slides out, then this drops below it.
            // Leaving reverses, so the button is gone before the address
            // retracts over it.
            transition={{ ...transition, delay: enabled ? 0.12 : 0 }}
          >
            <div className="bb-home__diag-action">
              <Button
                variant="secondary"
                onClick={runConnectionTest}
                disabled={connectionTest === 'running'}
              >
                {connectionTest === 'running'
                  ? strings.home.connectionTestButtonRunning
                  : strings.home.connectionTestButtonIdle}
              </Button>
              <DiagBadge state={connectionTest} />
            </div>

            {connectionSteps.length > 0 && (
              <ol className="bb-home__diag-steps">
                {connectionSteps.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
            )}
            {connectionError && (
              <p className="text-caption bb-home__diag-error">{connectionError}</p>
            )}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Both pushed to the bottom together, so the toggle keeps the optical
          centre of the screen no matter how much the diagnostics above expand. */}
      <div className="bb-home__footer">
        {/* Only when there is genuinely something to install. The check itself
            already ran at startup if «Проверять при запуске» is on - this reads
            its result rather than starting a second one, so it costs nothing
            and stays silent when the setting is off. */}
        <AnimatePresence>
          {zapretUpdate?.updateAvailable && (
            <motion.div
              className="bb-home__update"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: 'auto' }}
              exit={{ opacity: 0, height: 0 }}
              transition={transition}
            >
              <span className="text-caption">
                {t(strings.home.zapretUpdateAvailable, {
                  latest: zapretUpdate.latest ?? '',
                })}
              </span>
              {/* Was fire-and-forget: it started the job and reported nothing,
                  so a click looked like a dead button. Now it reports its own
                  state and hands the user to Settings, which owns the progress
                  modal and the restart prompt. */}
              <Button
                variant="secondary"
                disabled={updateStarting}
                onClick={startZapretUpdate}
              >
                {updateStarting
                  ? strings.home.zapretUpdateStarting
                  : strings.home.zapretUpdateButton}
              </Button>
              {updateError && (
                <span className="text-caption bb-home__error">{updateError}</span>
              )}
            </motion.div>
          )}
        </AnimatePresence>

        <Button variant="secondary" onClick={() => setShowInstructions(true)}>
          {strings.home.howToConnectButton}
        </Button>
      </div>

      <AnimatePresence>
        {showInstructions && (
          <Modal title={strings.home.instructionsModalTitle} onClose={() => setShowInstructions(false)}>
            <ConnectGuide launchOption={launchOption} address={address} />
          </Modal>
        )}
      </AnimatePresence>

    </div>
  )
}
