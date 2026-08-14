import { Modal } from './Modal'
import { IconFormDocument, IconGithub } from './icons'
import { useStrings } from '../lib/strings'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { callBridge, isNativeBridgeAvailable } from '../lib/bridge'
import './BugReportModal.css'

const GITHUB_ISSUES_URL = 'https://github.com/getonjbghelp/bridgebox/issues'

// One form per language rather than one form with a language field - a
// reporter filling this out is usually already fighting a DPI-blocked game,
// the last thing that helps is asking them to also pick a language first.
const GOOGLE_FORMS_URL: Record<'ru' | 'en', string> = {
  ru: 'https://forms.gle/fdhmR8C75JurYo429',
  en: 'https://forms.gle/MnsAV3GRZuA3qh6Y8',
}

/**
 * The sidebar's megaphone button opens this - two ways to reach the
 * developer, GitHub for anyone comfortable there, a form for anyone who
 * isn't. Both links go through the backend's open_external_url rather than
 * a plain `<a target="_blank">`: WebView2's handling of a new-window
 * navigation is not guaranteed to leave the app's own window at all, and
 * these two specifically have to land in the user's real browser.
 */
export function BugReportModal({ onClose }: { onClose: () => void }) {
  const strings = useStrings()
  const { locale } = useMotionPrefs()

  function openExternal(url: string) {
    if (!isNativeBridgeAvailable()) return
    callBridge('open_external_url', url).catch(() => {})
  }

  return (
    <Modal title={strings.sidebar.bugReportTooltip} onClose={onClose}>
      <p className="text-body bb-bugreport__intro">{strings.sidebar.bugReportIntro}</p>
      <div className="bb-bugreport__actions">
        <button
          type="button"
          className="bb-bugreport__platform"
          onClick={() => openExternal(GITHUB_ISSUES_URL)}
        >
          <IconGithub size={28} />
          <span>{strings.sidebar.bugReportGithubLabel}</span>
        </button>
        <button
          type="button"
          className="bb-bugreport__platform"
          onClick={() => openExternal(GOOGLE_FORMS_URL[locale])}
        >
          <IconFormDocument size={28} />
          <span>{strings.sidebar.bugReportFormsLabel}</span>
        </button>
      </div>
    </Modal>
  )
}
