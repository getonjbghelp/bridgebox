import { useEffect, useState } from 'react'
import { ClosingOverlay } from './components/ClosingOverlay'
import { IntegrityBanner } from './components/IntegrityBanner'
import { AppUpdateBanner } from './components/AppUpdateBanner'
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
  const [prewarming, setPrewarming] = useState(false)
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
    // #root itself crossfades in under the skeleton (see index.html's
    // #root.bb-root--visible) - toggled in the same effect, not a separate
    // one, so the two fades start on the same frame rather than racing.
    document.getElementById('root')?.classList.add('bb-root--visible')
    const boot = document.getElementById('bb-boot')
    if (!boot) return
    boot.classList.add('bb-boot--done')
    // Matches index.html's #bb-boot.bb-boot--done transition (500ms) plus a
    // small margin - removing the node before its own fade finishes would
    // cut the crossfade short instead of letting it complete.
    const id = window.setTimeout(() => boot.remove(), 520)
    return () => window.clearTimeout(id)
  }, [setupComplete])

  // Pay every screen's first layout and paint at boot, while the user is
  // still looking at Home, instead of on the first navigation to each.
  //
  // A screen that has never been displayed has no layout at all - display:
  // none skips it entirely - so the first switch to one costs a full layout
  // and paint of its whole subtree. Measured on the production build: 66ms
  // for Info and 76ms for Settings on the first visit, against 15ms on every
  // visit after. That first-visit cost lands on exactly the frames the
  // entrance animation should be drawing, so the animation starts several
  // frames in - or, on a machine busy with the backend's own startup work,
  // late enough that it reads as not playing at all. Warming them here makes
  // the first switch cost what the second one already did.
  //
  // Started only after the boot-skeleton crossfade above has finished, not
  // in the same tick as it (the naive version of this effect) - setting
  // data-prewarm forces three full screens through layout and paint
  // synchronously, and doing that while #root/#bb-boot's own opacity
  // transition is trying to draw its first frames starved that transition
  // of exactly the frames it needed: the skeleton→app handover read as a
  // hard cut instead of the fade its own CSS asks for, reproducibly, every
  // launch. The wizard has no prewarming at all and never showed this -
  // that contrast is what pointed at this effect rather than the CSS.
  useEffect(() => {
    if (setupComplete === null) return
    let stopId: number | undefined
    const startId = window.setTimeout(() => {
      setPrewarming(true)
      // Two frames is enough for style, layout and paint to have run; this
      // timeout is only a floor on that, not a guess at how long it takes.
      stopId = window.setTimeout(() => setPrewarming(false), 500)
    }, 520)
    return () => {
      window.clearTimeout(startId)
      if (stopId !== undefined) window.clearTimeout(stopId)
    }
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
    <div className="bb-app-shell" data-prewarm={prewarming}>
      <IntegrityBanner />
      <AppUpdateBanner />
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
        {/* aria-hidden only while warming: a display:none screen is already
            out of the accessibility tree, but a prewarmed one is not, and
            three invisible copies of the app briefly announcing themselves
            is worse than the layout cost this avoids. */}
        <div
          className="bb-app__screen"
          data-active={screen === 'home'}
          aria-hidden={(prewarming && screen !== 'home') || undefined}
        >
          <HomeScreen />
        </div>
        <div
          className="bb-app__screen"
          data-active={screen === 'settings'}
          aria-hidden={(prewarming && screen !== 'settings') || undefined}
        >
          <SettingsScreen />
        </div>
        <div
          className="bb-app__screen bb-app__screen--wide"
          data-active={screen === 'logs'}
          aria-hidden={(prewarming && screen !== 'logs') || undefined}
        >
          {/* Mounted while hidden, so it must be told not to keep polling. */}
          <LogsScreen active={screen === 'logs'} />
        </div>
        <div
          className="bb-app__screen"
          data-active={screen === 'info'}
          aria-hidden={(prewarming && screen !== 'info') || undefined}
        >
          <InfoScreen />
        </div>
      </main>
      </div>
      <ClosingOverlay />
    </div>
  )
}

export default App
