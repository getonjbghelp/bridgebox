import { AnimatePresence } from 'framer-motion'
import { useEffect, useRef, useState } from 'react'
import { BrandLogo } from '../components/BrandLogo'
import { Modal } from '../components/Modal'
import { PeopleCredits } from '../components/PeopleCredits'
import { Section, Row } from '../components/Section'
import { LINK_ICONS } from '../components/icons'
import { ABOUT, aboutText, localeText, type AboutLink } from '../lib/content'
import { renderRich } from '../lib/richText'
import { useStrings, t } from '../lib/strings'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { callBridge, isNativeBridgeAvailable, waitForBridgeReady } from '../lib/bridge'
import './InfoScreen.css'

interface AppInfo {
  ok: boolean
  version: string
  /** "b1" while pre-release, "" once a final version ships - same source
   *  BetaBadge reads, never duplicated here. */
  label: string
  /** Empty for an ordinary checkout or build. */
  channel: string
}

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

/**
 * "About" - logo, a short description, the live version and file-integrity
 * state (both read fresh from the backend, never duplicated as static text),
 * the licence, and whatever contact links about.json's `links` array holds.
 *
 * Deliberately a read-only mirror of state that already exists elsewhere
 * (BetaBadge's version, IntegrityBanner's check) rather than a second source
 * of truth for either - see lib/content.ts and Api.app_info/integrity_status.
 */
export function InfoScreen({ active }: { active: boolean }) {
  const strings = useStrings()
  const { locale } = useMotionPrefs()
  const [info, setInfo] = useState<AppInfo | null>(null)
  const [integrity, setIntegrity] = useState<IntegrityStatus | null>(null)
  const [openLink, setOpenLink] = useState<AboutLink | null>(null)
  // The Links row (custom-SVG icons via dangerouslySetInnerHTML) and
  // PeopleCredits are the priciest things this screen renders, and neither
  // is needed for the entrance to read as smooth - only the header/logo/
  // version block is. All four screens mount together at boot (App.tsx),
  // hidden behind display:none, so a plain mount-time effect fires at boot
  // too - long before the user ever navigates here - and defers nothing
  // useful. `active` is what actually marks the moment this screen goes
  // display:none -> block, which is also the FIRST time the browser lays
  // this subtree out at all (a display:none element has no layout to warm
  // up from). Rendering the heavy parts one frame after THAT, once and
  // never again (the ref), lets .bb-app__screen's entrance animation get a
  // cheap first frame instead of racing a layout pass sized for the whole
  // screen - subsequent visits are already warm, per the fix's own
  // measurement (see PR notes), so there is nothing left to defer by then.
  const [showExtras, setShowExtras] = useState(false)
  const deferredOnce = useRef(false)
  useEffect(() => {
    if (!active || deferredOnce.current) return
    deferredOnce.current = true
    const id = requestAnimationFrame(() => setShowExtras(true))
    return () => cancelAnimationFrame(id)
  }, [active])

  useEffect(() => {
    waitForBridgeReady().then(() => {
      if (!isNativeBridgeAvailable()) return
      callBridge<AppInfo>('app_info')
        .then((result) => setInfo(result))
        .catch(() => {})
      // Reads the SAME cached check IntegrityBanner does - integrity.py's
      // baseline runs once at startup, this just asks for the answer, it
      // does not trigger a second hash pass.
      callBridge<IntegrityStatus>('integrity_status')
        .then((result) => setIntegrity(result))
        .catch(() => {})
    })
  }, [])

  const about = aboutText(locale)

  return (
    <div>
      <h1 className="text-display" style={{ marginBottom: 'var(--space-6)' }}>
        {strings.info.title}
      </h1>

      <div className="bb-info__mark">
        <BrandLogo title={strings.sidebar.brandName} />
      </div>
      <p className="text-body bb-info__tagline">{renderRich(about.description)}</p>

      <Section title={strings.info.detailsTitle}>
        <Row
          label={strings.info.versionLabel}
          control={
            <span className="text-mono">
              {info?.version || '—'}
              {info?.label ? ` (${info.label})` : ''}
            </span>
          }
        />
        <Row
          label={strings.info.integrityTitle}
          control={<span className="text-caption">{integrityStatusText(integrity, strings)}</span>}
        />
        <Row
          label={strings.info.licenseLabel}
          control={<span className="text-caption">{about.license.name}</span>}
        />
        {info?.channel && (
          <Row
            label={strings.info.sourceLabel}
            control={<span className="text-caption">{strings.info.sourcePublic}</span>}
          />
        )}
      </Section>
      <p className="text-caption bb-info__license-text">{renderRich(about.license.text)}</p>

      {/* Nothing to show until build_content.py has written at least one link -
          an empty "Ссылки" heading over a blank box is worse than no section. */}
      {showExtras && ABOUT.links.length > 0 && (
        <Section title={strings.info.linksTitle}>
          <div className="bb-info__links">
            {ABOUT.links.map((link) => {
              const hint = localeText(link.label, locale)
              const icon = <LinkIcon link={link} />
              // action decides behaviour, not field presence - see AboutLink's
              // docstring in lib/content.ts.
              if (link.action === 'popup') {
                return (
                  <button
                    key={link.id}
                    type="button"
                    className="bb-info__link"
                    title={hint}
                    aria-label={hint}
                    onClick={() => setOpenLink(link)}
                  >
                    {icon}
                  </button>
                )
              }
              return (
                <a
                  key={link.id}
                  className="bb-info__link"
                  href={link.url}
                  title={hint}
                  aria-label={hint}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {icon}
                </a>
              )
            })}
          </div>
        </Section>
      )}

      {showExtras && <PeopleCredits />}

      <AnimatePresence>
        {openLink && (
          <Modal title={localeText(openLink.popupTitle, locale)} onClose={() => setOpenLink(null)}>
            <p className="text-body">{renderRich(localeText(openLink.popupText, locale))}</p>
            {openLink.popupUrl && (
              <a
                className="bb-info__popup-link"
                href={openLink.popupUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                {localeText(openLink.popupUrlLabel, locale) || openLink.popupUrl}
              </a>
            )}
          </Modal>
        )}
      </AnimatePresence>
    </div>
  )
}

/** Either a registry glyph or the raw markup build_content.py's SVG importer
 * wrote to iconSvg - see AboutLink's docstring for why exactly one applies. */
function LinkIcon({ link }: { link: AboutLink }) {
  if (link.icon === 'custom') {
    if (!link.iconSvg) return null
    return <span className="bb-info__custom-icon" dangerouslySetInnerHTML={{ __html: link.iconSvg }} />
  }
  const Icon = LINK_ICONS[link.icon as keyof typeof LINK_ICONS]
  return Icon ? <Icon size={18} /> : null
}

function integrityStatusText(
  status: IntegrityStatus | null,
  strings: ReturnType<typeof useStrings>,
): string {
  if (!status || status.baselineMissing) return strings.info.integrityChecking
  if (status.verified) return strings.info.integrityVerified
  return t(strings.info.integrityChanged, { count: status.total })
}
