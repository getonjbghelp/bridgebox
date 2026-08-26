import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Button } from './Button'
import { Modal } from './Modal'
import { CHANGELOG, changelogText, type ChangeLevel } from '../lib/content'
import { useSpringTransition } from '../lib/motion'
import { renderChangelogBody } from '../lib/richText'
import { useStrings, t } from '../lib/strings'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { callBridge, isNativeBridgeAvailable, waitForBridgeReady } from '../lib/bridge'
import './BetaBadge.css'

interface AppInfo {
  ok: boolean
  version: string
  /** "b1" while pre-release, "" once a final version ships. */
  label: string
}

/** Grace period on mouseleave.
 *
 *  Without it the popup closed the instant the cursor left the chip, so the
 *  «История версий» button inside it was unreachable - you had to cross the
 *  gap between chip and panel, and the panel was gone before you arrived.
 *  Cancelled if the cursor lands on the panel itself. */
const CLOSE_DELAY_MS = 260

/**
 * The β mark beside the wordmark, its explanation, and the version history
 * behind it.
 *
 * Renders nothing at all when the running version has no pre-release segment -
 * the badge removing itself on release is what its own tooltip promises, so it
 * is derived rather than switched off by hand later. See backend version.py.
 *
 * The tooltip is hover-and-focus on a real button rather than a `title=`
 * attribute (which is what the sidebar uses): this one carries a version
 * number and a button, and a native tooltip can hold neither.
 */
export function BetaBadge() {
  const strings = useStrings()
  const { locale } = useMotionPrefs()
  const [info, setInfo] = useState<AppInfo | null>(null)
  const [open, setOpen] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const closeTimer = useRef<number | undefined>(undefined)
  const transition = useSpringTransition()

  const cancelClose = () => {
    if (closeTimer.current !== undefined) {
      window.clearTimeout(closeTimer.current)
      closeTimer.current = undefined
    }
  }
  const openNow = () => {
    cancelClose()
    setOpen(true)
  }
  const closeSoon = () => {
    cancelClose()
    closeTimer.current = window.setTimeout(() => setOpen(false), CLOSE_DELAY_MS)
  }

  // A pending timer firing after unmount would set state on a dead component.
  useEffect(() => cancelClose, [])

  useEffect(() => {
    waitForBridgeReady().then(() => {
      if (!isNativeBridgeAvailable()) {
        // Plain-browser preview: show the badge so it can be designed, with
        // the version the changelog's newest entry claims.
        setInfo({ ok: true, version: '0.1', label: 'b1' })
        return
      }
      callBridge<AppInfo>('app_info')
        .then((result) => setInfo(result))
        .catch(() => {})
    })
  }, [])

  if (!info?.label) return null

  return (
    <span className="bb-beta">
      <button
        type="button"
        className="bb-beta__chip"
        aria-expanded={open}
        aria-label={strings.home.betaTooltipTitle}
        onMouseEnter={openNow}
        onMouseLeave={closeSoon}
        onFocus={openNow}
        onBlur={closeSoon}
        onClick={() => setHistoryOpen(true)}
      >
        {/* β, not the word. It is the universally understood mark for this,
            and it fits the wordmark's line without competing with it. */}
        <span aria-hidden="true">{strings.home.betaBadge}</span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.span
            className="bb-beta__tip"
            role="tooltip"
            // Keeps the panel open while the cursor is on it - the whole point
            // of the delay above is that this is reachable.
            onMouseEnter={openNow}
            onMouseLeave={closeSoon}
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={transition}
          >
            <span className="bb-beta__tip-title">{strings.home.betaTooltipTitle}</span>
            <span className="bb-beta__tip-body">{strings.home.betaTooltipBody}</span>
            <span className="bb-beta__tip-version text-mono">
              {t(strings.home.betaVersionLine, { version: info.version })}
            </span>
            <Button variant="secondary" onClick={() => setHistoryOpen(true)}>
              {strings.home.betaHistoryButton}
            </Button>
          </motion.span>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {historyOpen && (
          <Modal
            title={strings.home.changelogModalTitle}
            onClose={() => setHistoryOpen(false)}
            maxWidth={560}
          >
            <ol className="bb-changelog">
              {CHANGELOG.map((entry) => {
                const text = changelogText(entry, locale)
                return (
                  <li key={entry.version} className="bb-changelog__entry">
                    <div className="bb-changelog__head">
                      <span className="text-subtitle">{text.title}</span>
                      <span className={`bb-changelog__badge bb-changelog__badge--${entry.level}`}>
                        {levelLabel(entry.level, strings)}
                      </span>
                    </div>
                    <p className="text-caption bb-changelog__meta">
                      <span className="text-mono">{entry.version}</span>
                      {' · '}
                      {formatDate(entry.date, locale)}
                    </p>
                    <div className="bb-changelog__body bb-prose">{renderChangelogBody(text.body)}</div>
                  </li>
                )
              })}
            </ol>
          </Modal>
        )}
      </AnimatePresence>
    </span>
  )
}

function levelLabel(level: ChangeLevel, strings: ReturnType<typeof useStrings>): string {
  if (level === 'critical') return strings.home.changelogLevelCritical
  if (level === 'major') return strings.home.changelogLevelMajor
  return strings.home.changelogLevelMinor
}

/** ISO in the data file, a locale-appropriate date on screen. */
function formatDate(iso: string, locale: 'ru' | 'en'): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleDateString(locale === 'ru' ? 'ru-RU' : 'en-US', {
    day: 'numeric',
    month: 'long',
    year: 'numeric',
  })
}
