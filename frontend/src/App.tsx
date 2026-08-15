import { useEffect, useState } from 'react'
import { ClosingOverlay } from './components/ClosingOverlay'
import { IntegrityBanner } from './components/IntegrityBanner'
import { Sidebar } from './components/Sidebar'
import { HomeScreen } from './screens/HomeScreen'
import { SettingsScreen } from './screens/SettingsScreen'
import { LogsScreen } from './screens/LogsScreen'
import { InfoScreen } from './screens/InfoScreen'
import { SetupWizard } from './screens/SetupWizard'
import { useMotionPrefs } from './state/MotionPrefsContext'
import './App.css'

export type Screen = 'home' | 'settings' | 'logs' | 'info'

// Dev-only door back into the wizard. In a plain browser there is no config
// to read, so setupComplete resolves to true and the wizard would otherwise
// be unreachable for design work without faking a bridge.
const forceSetup =
  typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('setup')

function App() {
  const [screen, setScreen] = useState<Screen>('home')
  const { setupComplete } = useMotionPrefs()

  // Take the boot skeleton down, but only once there is something behind it.
  //
  // index.html paints that skeleton before the bundle is even fetched, which
  // is the only way to cover WebView2 start-up and the module parse - no React
  // code has run yet at that point. Dismissing it on mount would therefore
  // trade a skeleton for a blank window, because App renders nothing at all
  // until setupComplete is known.
  useEffect(() => {
    if (setupComplete === null) return
    const boot = document.getElementById('bb-boot')
    if (!boot) return
    boot.classList.add('bb-boot--done')
    const id = window.setTimeout(() => boot.remove(), 200)
    return () => window.clearTimeout(id)
  }, [setupComplete])

  // null means get_config hasn't answered yet. Rendering the app here and
  // swapping to the wizard a frame later is a flash of the thing a first-run
  // user is specifically not supposed to see yet, so neither is drawn until
  // the answer is in - the skeleton above is what covers the gap.
  if (setupComplete === null) return null
  // Outside the app shell AND outside the wizard: a close can be requested
  // from either, and the overlay has to be able to cover both.
  if (forceSetup || !setupComplete)
    return (
      <>
        <IntegrityBanner />
        <SetupWizard />
        <ClosingOverlay />
      </>
    )

  return (
    // .bb-app is a row (sidebar beside content), so the banner needs its own
    // column wrapper to sit ABOVE that row rather than become a third column
    // in it.
    <div className="bb-app-shell">
      <IntegrityBanner />
      <div className="bb-app">
        <Sidebar active={screen} onSelect={setScreen} />
      {/*
        All three screens stay mounted; only the active one is displayed.
        Navigating away used to unmount the screen, which discarded the log
        buffer and its scroll position, the connection-test results, and any
        setting typed but not yet committed - state the user had no reason to
        expect a tab switch to destroy.

        The enter animation is CSS rather than AnimatePresence: an element
        going from display:none back to display:block replays its animations
        for free, and mode="wait" had to hold the outgoing screen for a full
        exit before the incoming one could mount, which is exactly the delay
        this change removes.
      */}
      <main className="bb-app__content">
        <div className="bb-app__screen" data-active={screen === 'home'}>
          <HomeScreen />
        </div>
        <div className="bb-app__screen" data-active={screen === 'settings'}>
          <SettingsScreen />
        </div>
        <div className="bb-app__screen" data-active={screen === 'logs'}>
          {/* Mounted while hidden, so it must be told not to keep polling. */}
          <LogsScreen active={screen === 'logs'} />
        </div>
        <div className="bb-app__screen" data-active={screen === 'info'}>
          <InfoScreen active={screen === 'info'} />
        </div>
      </main>
      </div>
      <ClosingOverlay />
    </div>
  )
}

export default App
