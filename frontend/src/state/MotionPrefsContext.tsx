import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { callBridge, isNativeBridgeAvailable, waitForBridgeReady } from '../lib/bridge'

/** The stored preference. "system" means "whatever Windows is set to". */
export type LanguagePref = 'system' | 'ru' | 'en'
/** What actually gets shown - "system" already resolved to a real locale. */
export type Locale = 'ru' | 'en'

/** navigator.language ("ru-RU", "en-US", "de-DE"...) collapsed to a locale
 * this app ships strings for. Anything that is not Russian falls back to
 * English rather than guessing - a wrong guess is worse than the safe
 * default for a language nobody asked for. */
function detectSystemLocale(): Locale {
  const lang = typeof navigator !== 'undefined' ? navigator.language : ''
  return lang.toLowerCase().startsWith('ru') ? 'ru' : 'en'
}

// Everything under config.yaml's `ui:` section. Named for motion because that
// was all it held at first; theme and the sidebar state ride along rather
// than each growing a second way to reach the same config file.
interface MotionPrefs {
  animationsEnabled: boolean
  setAnimationsEnabled: (value: boolean) => void
  /** The one duration every hook in lib/motion.ts scales its own transition
   *  against - see UiConfig.animation_duration_ms's own docstring. */
  animationDurationMs: number
  setAnimationDurationMs: (value: number) => void
  theme: 'light' | 'dark'
  setTheme: (value: 'light' | 'dark') => void
  sidebarCollapsed: boolean
  setSidebarCollapsed: (value: boolean) => void
  /**
   * Has the first-run wizard been finished? `null` means the config has not
   * come back yet - three states rather than two on purpose: defaulting the
   * unknown case to `false` flashes the whole wizard for a frame on every
   * single launch, and defaulting it to `true` flashes the app at somebody
   * who has never seen it. App.tsx renders neither until this is known.
   */
  setupComplete: boolean | null
  setSetupComplete: (value: boolean) => void
  /** The raw preference, for a language picker to know what is selected. */
  language: LanguagePref
  /** The resolved locale, for strings.ts to pick a catalog by. */
  locale: Locale
  setLanguage: (value: LanguagePref) => void
}

// Exported so callers that read the full config response (Settings' factory
// reset) can type its `ui` field without re-typing these fields a second
// time.
export interface UiSection {
  theme: 'light' | 'dark'
  animations_enabled: boolean
  animation_duration_ms: number
  sidebar_collapsed: boolean
  setup_complete: boolean
  language: LanguagePref
}

interface ConfigResponse {
  ok: boolean
  config: { ui: UiSection } | null
}

const MotionPrefsContext = createContext<MotionPrefs | null>(null)

export function MotionPrefsProvider({ children }: { children: ReactNode }) {
  const [animationsEnabled, setAnimationsEnabledState] = useState(true)
  // Matches UiConfig.animation_duration_ms's own default.
  const [animationDurationMs, setAnimationDurationMsState] = useState(220)
  // Matches UiConfig.theme's schema default (and index.html's boot-skeleton
  // fallback) so this initial value never fights the hint the skeleton
  // already painted before React took over.
  const [theme, setThemeState] = useState<'light' | 'dark'>('dark')
  // Matches UiConfig.sidebar_collapsed's default, so the rail does not start
  // expanded and snap shut a moment later when get_config lands.
  const [sidebarCollapsed, setSidebarCollapsedState] = useState(true)
  const [setupComplete, setSetupCompleteState] = useState<boolean | null>(null)
  const [language, setLanguageState] = useState<LanguagePref>('system')
  // Resolved synchronously from navigator.language, before get_config has
  // even had a chance to answer - nothing waits on a config round trip to
  // know what language to render the wizard's first screen in.
  const [locale, setLocaleState] = useState<Locale>(detectSystemLocale)

  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)')
    if (query.matches) setAnimationsEnabledState(false)
  }, [])

  // Seed from the persisted config.yaml (via Api.get_config) once on mount -
  // theme/animations are real settings now, not just in-memory React state.
  useEffect(() => {
    waitForBridgeReady().then(() => {
      // Plain-browser dev mode: no config to read, and leaving setupComplete
      // at null would leave App.tsx rendering nothing forever. The wizard is
      // reachable there via ?setup=1 (see App.tsx) rather than by default,
      // so previewing any other screen doesn't have to click through it.
      if (!isNativeBridgeAvailable()) {
        setSetupCompleteState(true)
        return
      }
      callBridge<ConfigResponse>('get_config')
        .then((result) => {
          if (result.ok && result.config) {
            setThemeState(result.config.ui.theme)
            setAnimationsEnabledState(result.config.ui.animations_enabled)
            setAnimationDurationMsState(result.config.ui.animation_duration_ms)
            setSidebarCollapsedState(result.config.ui.sidebar_collapsed)
            setSetupCompleteState(result.config.ui.setup_complete)
            const pref = result.config.ui.language ?? 'system'
            setLanguageState(pref)
            setLocaleState(pref === 'system' ? detectSystemLocale() : pref)
          } else {
            setSetupCompleteState(true)
          }
        })
        // Without this the failure was an unhandled rejection: the theme
        // silently stayed at the React default instead of the saved one,
        // with nothing in the console pointing at why.
        .catch((err) => {
          console.error('get_config failed, keeping default theme:', err)
          // A config we could not read is not evidence of a first run, and
          // showing the wizard on every launch would be worse than skipping
          // it - the app itself still works with defaults.
          setSetupCompleteState(true)
        })
    })
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    // A hint for the boot skeleton in index.html, which has to paint before
    // any of this has run - see the inline script there. NOT a second source
    // of truth: config.yaml still decides, this only records what it last
    // said so the first frame is not white on a dark-theme machine. A stale or
    // missing hint self-corrects the moment get_config answers.
    try {
      localStorage.setItem('bb-theme-hint', theme)
    } catch {
      /* storage disabled - the skeleton falls back to light */
    }
  }, [theme])

  // The gate was JS-only, reachable through useSpringTransition and therefore
  // only by framer-motion. A CSS @keyframes or transition had no way to see
  // it, so anything animated in CSS kept moving after the user turned
  // animations off. Mirroring it onto the root element costs one line and
  // makes the setting mean the same thing on both sides.
  useEffect(() => {
    document.documentElement.dataset.animations = animationsEnabled ? 'on' : 'off'
  }, [animationsEnabled])

  function setTheme(value: 'light' | 'dark') {
    setThemeState(value)
    if (isNativeBridgeAvailable()) {
      callBridge('update_config', { ui: { theme: value } }).catch(() => {})
    }
  }

  function setAnimationsEnabled(value: boolean) {
    setAnimationsEnabledState(value)
    if (isNativeBridgeAvailable()) {
      callBridge('update_config', { ui: { animations_enabled: value } }).catch(() => {})
    }
  }

  function setAnimationDurationMs(value: number) {
    setAnimationDurationMsState(value)
    if (isNativeBridgeAvailable()) {
      callBridge('update_config', { ui: { animation_duration_ms: value } }).catch(() => {})
    }
  }

  function setSidebarCollapsed(value: boolean) {
    setSidebarCollapsedState(value)
    if (isNativeBridgeAvailable()) {
      callBridge('update_config', { ui: { sidebar_collapsed: value } }).catch(() => {})
    }
  }

  // Same shape as the three setters above, and for the same reason: flips the
  // local value first so the wizard appears or leaves immediately, then
  // persists. The write is what decides whether the NEXT launch runs setup;
  // the current one has no reason to sit behind a disk round trip.
  function setSetupComplete(value: boolean) {
    setSetupCompleteState(value)
    if (isNativeBridgeAvailable()) {
      callBridge('update_config', { ui: { setup_complete: value } }).catch(() => {})
    }
  }

  // Same optimistic-then-persist shape, but the local state that actually
  // drives strings.ts is `locale`, not the raw `language` preference - a
  // switch to "system" has to re-resolve immediately, not wait for whatever
  // Windows reports to round-trip through config.yaml first.
  function setLanguage(value: LanguagePref) {
    setLanguageState(value)
    setLocaleState(value === 'system' ? detectSystemLocale() : value)
    if (isNativeBridgeAvailable()) {
      callBridge('update_config', { ui: { language: value } }).catch(() => {})
    }
  }

  return (
    <MotionPrefsContext.Provider
      value={{
        animationsEnabled,
        setAnimationsEnabled,
        animationDurationMs,
        setAnimationDurationMs,
        theme,
        setTheme,
        sidebarCollapsed,
        setSidebarCollapsed,
        setupComplete,
        setSetupComplete,
        language,
        locale,
        setLanguage,
      }}
    >
      {children}
    </MotionPrefsContext.Provider>
  )
}

export function useMotionPrefs(): MotionPrefs {
  const ctx = useContext(MotionPrefsContext)
  if (!ctx) throw new Error('useMotionPrefs must be used within MotionPrefsProvider')
  return ctx
}
