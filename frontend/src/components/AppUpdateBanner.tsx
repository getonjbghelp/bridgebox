import { useEffect, useRef, useState } from 'react'
import { AnimatePresence } from 'framer-motion'
import { Button } from './Button'
import { Modal } from './Modal'
import { DiagBadge, type DiagState } from './DiagBadge'
import { useStrings, t } from '../lib/strings'
import { renderChangelogBody } from '../lib/richText'
import { pickReleaseNotes } from '../lib/releaseNotes'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { callBridge, isNativeBridgeAvailable, logBridgeError, waitForBridgeReady } from '../lib/bridge'
import { clearPoll } from '../lib/poll'
import './AppUpdateBanner.css'

/**
 * BridgeBox's own update check (app_update.py / desktop.Api.start_app_update_check),
 * not zapret's (that one is HomeScreen's own inline banner - different repo,
 * different config section, different urgency).
 *
 * Mounted once, above the whole app (see App.tsx, next to IntegrityBanner) -
 * a critical release has to stay visible no matter which screen the user is
 * on, the same reasoning that put the integrity warning there.
 */
interface AppUpdateCheck {
  ok: boolean
  started: boolean
  done: boolean
  installed: string | null
  latest: string | null
  notes: string | null
  htmlUrl: string | null
  critical: boolean
  updateAvailable: boolean
  dismissedVersion: string
}

interface AppApplyProgress {
  started: boolean
  done: boolean
  ok: boolean | null
  error: string | null
  version: string | null
}

const POLL_MS = 1500
const MAX_POLLS = 20
const APPLY_POLL_MS = 1000

// How often a still-unaddressed CRITICAL update re-surfaces the modal after
// the user has already closed it once. Not shown for an ordinary update at
// all - see handleClose. 25 minutes: infrequent enough to read as a nudge,
// not a nag, over a session that is usually measured in hours of play.
const CRITICAL_REMINDER_MS = 25 * 60 * 1000

export function AppUpdateBanner() {
  const strings = useStrings()
  // Releases carry both languages in one body (see lib/releaseNotes.ts). This
  // is the resolved locale the app is actually being read in, not the stored
  // "system" preference, so it matches the rest of the interface around it.
  const { locale } = useMotionPrefs()
  const [check, setCheck] = useState<AppUpdateCheck | null>(null)
  const [modalOpen, setModalOpen] = useState(false)
  // Guards the FIRST automatic open only - re-opens after that (the critical
  // reminder timer, or the banner's own "Подробнее") go through setModalOpen
  // directly and must not be blocked by this.
  const [autoOpened, setAutoOpened] = useState(false)

  // Self-update (download + swap the running .exe) state - separate from the
  // check above, which only asks "is there something newer". applyState
  // 'error' covers both a real download failure AND dev mode (no single .exe
  // to swap) - either way the fallback is the same: send the user to the
  // release page by hand.
  const [applyState, setApplyState] = useState<DiagState>('idle')
  const [applyError, setApplyError] = useState<string | null>(null)
  const applyPollRef = useRef<number | undefined>(undefined)

  useEffect(() => () => stopApplyPolling(), [])

  function stopApplyPolling() {
    clearPoll(applyPollRef)
  }

  useEffect(() => {
    let cancelled = false
    let attempts = 0
    let id: number | undefined

    async function poll() {
      if (cancelled || !isNativeBridgeAvailable()) return
      try {
        const result = await callBridge<AppUpdateCheck>('app_update_check')
        if (cancelled) return
        setCheck(result)
        if (!result.started || result.done || ++attempts >= MAX_POLLS) {
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

  // Open the modal once, automatically, the first time a fresh result says
  // there is something to show - an ordinary update the user has already
  // dismissed (dismissedVersion) stays quiet; a critical one never does.
  useEffect(() => {
    if (!check?.done || !check.ok || !check.updateAvailable || autoOpened) return
    if (!check.critical && check.latest === check.dismissedVersion) return
    setModalOpen(true)
    setAutoOpened(true)
  }, [check, autoOpened])

  // The periodic nudge - critical only. An ordinary update never re-opens
  // itself; the user already said "not now" by closing it once.
  useEffect(() => {
    if (!check?.critical || !check.updateAvailable) return
    const id = window.setInterval(() => setModalOpen(true), CRITICAL_REMINDER_MS)
    return () => window.clearInterval(id)
  }, [check?.critical, check?.updateAvailable])

  function handleClose() {
    setModalOpen(false)
    // Persisted (survives a restart) only for a non-critical update - a
    // critical one must keep nagging even after its modal is dismissed once,
    // via the reminder timer above, or "critical" would mean nothing. See
    // desktop.Api.dismiss_app_update.
    if (check && !check.critical && check.latest && isNativeBridgeAvailable()) {
      callBridge('dismiss_app_update', check.latest).catch(() => {})
    }
  }

  async function handleUpdateNow() {
    if (!isNativeBridgeAvailable()) return
    setApplyState('running')
    setApplyError(null)
    await callBridge('start_app_apply_update').catch(() => {})
    stopApplyPolling()
    applyPollRef.current = window.setInterval(async () => {
      try {
        const progress = await callBridge<AppApplyProgress>('app_apply_progress')
        if (!progress.done) return
        stopApplyPolling()
        if (progress.ok) {
          setApplyState('done')
        } else {
          setApplyState('error')
          setApplyError(progress.error)
        }
      } catch (err) {
        logBridgeError(err)
        stopApplyPolling()
        setApplyState('error')
        setApplyError(strings.common.unexpectedError)
      }
    }, APPLY_POLL_MS)
  }

  function handleRestartNow() {
    // Not restart_app: applying a self-update needs the dedicated relaunch
    // helper (see Api.restart_after_app_update) since the swap cannot
    // happen in this process - see app_update.py's own docstring for why.
    if (isNativeBridgeAvailable()) callBridge('restart_after_app_update').catch(() => {})
  }

  function handleOpenReleasePage() {
    if (check?.htmlUrl && isNativeBridgeAvailable()) {
      callBridge('open_external_url', check.htmlUrl).catch(() => {})
    }
  }

  const showBanner = check?.critical && check.updateAvailable

  // Same three states everywhere the "update now" action appears (banner and
  // modal) - one place decides what the button row looks like, so a change
  // here cannot drift between the two.
  function renderApplyActions(danger: boolean) {
    if (applyState === 'running') {
      return <DiagBadge state="running" />
    }
    if (applyState === 'done') {
      return (
        <Button variant={danger ? 'danger' : 'primary'} onClick={handleRestartNow}>
          {strings.appUpdate.restartNow}
        </Button>
      )
    }
    if (applyState === 'error') {
      return (
        <>
          <span className="bb-update-modal__error">
            {t(strings.appUpdate.applyFailed, { error: applyError ?? '' })}
          </span>
          <Button variant="ghost" onClick={handleOpenReleasePage}>
            {strings.appUpdate.viewDetails}
          </Button>
        </>
      )
    }
    return (
      <Button variant={danger ? 'danger' : 'primary'} onClick={handleUpdateNow}>
        {strings.appUpdate.updateNow}
      </Button>
    )
  }

  return (
    <>
      {showBanner && (
        <div className="bb-update-banner" role="alert" data-critical="true">
          <div className="bb-update-banner__text">
            <strong className="bb-update-banner__title">
              {strings.appUpdate.criticalBannerTitle}
            </strong>
            <span className="bb-update-banner__body">
              {t(strings.appUpdate.criticalBannerBody, { latest: check!.latest ?? '' })}
            </span>
          </div>
          <div className="bb-update-banner__actions">
            <Button variant="ghost" onClick={() => setModalOpen(true)}>
              {strings.appUpdate.viewDetails}
            </Button>
            {renderApplyActions(true)}
          </div>
        </div>
      )}

      <AnimatePresence>
        {modalOpen && check && (
          <Modal
            title={t(strings.appUpdate.modalTitle, { latest: check.latest ?? '' })}
            onClose={handleClose}
            maxWidth={520}
          >
            {check.critical && (
              <p className="bb-update-modal__critical-tag">{strings.appUpdate.criticalTag}</p>
            )}
            <div className="bb-update-modal__notes bb-prose">
              {check.notes
                ? renderChangelogBody(pickReleaseNotes(check.notes, locale))
                : strings.appUpdate.noNotes}
            </div>
            <div className="bb-update-modal__actions">
              {applyState === 'idle' && (
                <Button variant="ghost" onClick={handleClose}>
                  {strings.appUpdate.later}
                </Button>
              )}
              {renderApplyActions(check.critical)}
            </div>
          </Modal>
        )}
      </AnimatePresence>
    </>
  )
}
