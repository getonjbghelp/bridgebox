import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/global.css'
import App from './App.tsx'
import { MotionPrefsProvider } from './state/MotionPrefsContext'
// ponytail: diagnostic build only - see lib/motionTrace.ts. Gated behind
// --tracer (see maybeInstallMotionTrace below) rather than always on: it is
// a permanent rAF loop plus an 8ms sampling timer, harmless to carry in
// every build but not something to run by default.
import { isNativeBridgeAvailable, waitForBridgeReady, callBridge } from './lib/bridge'
import { installMotionTrace } from './lib/motionTrace'

// Checked here, ahead of React, rather than from inside App: the tracer's
// whole value is catching the boot-skeleton crossfade (see App.tsx), which
// happens before React has rendered anything, so the check has to resolve
// before createRoot().render() for that window to be worth anything.
//
// The bridge is the source of truth (--tracer, read by desktop.py's Api),
// but it is not ready on this very first tick - window.pywebview.api gets
// injected a beat after the module graph runs. Waiting for it costs the
// tracer the literal first frames after module load, which is an honest
// trade for not risking the packaged build's asset loading by touching how
// the window's URL is constructed just to smuggle a flag into it.
//
// A plain browser tab (no pywebview host at all) has no --tracer to ask
// about - that is exactly how this tracer was driven all session while
// diagnosing the Info screen bug - so ?tracer=1 in the address bar is kept
// as the equivalent switch there.
async function maybeInstallMotionTrace(): Promise<void> {
  if (new URLSearchParams(window.location.search).has('tracer')) {
    installMotionTrace()
    return
  }
  await waitForBridgeReady()
  if (!isNativeBridgeAvailable()) return
  try {
    if (await callBridge<boolean>('tracer_enabled')) installMotionTrace()
  } catch {
    // Bridge present but the call failed (older build without the method,
    // or the host is still mid-injection) - default stays off.
  }
}

void maybeInstallMotionTrace()

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <MotionPrefsProvider>
      <App />
    </MotionPrefsProvider>
  </StrictMode>,
)
