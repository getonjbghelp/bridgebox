// Self-check for the pywebview bridge race. Run it with:
//
//   node --test test/bridge.test.ts        (from frontend/)
//
// No test framework - Node 23+ strips the types and ships its own runner.
//
// What this guards: pywebview creates window.pywebview.api and attaches its
// methods as two separate steps. Code that checked only "does `api` exist"
// saw a truthy object with no methods on it and failed permanently with
// "bridge method not implemented: bridge_status" - which also silently broke
// theme restore, because get_config lost the same race.

import assert from 'node:assert/strict'
import test from 'node:test'

type Win = {
  pywebview?: { api?: Record<string, (...args: unknown[]) => Promise<unknown>> }
  setInterval: typeof setInterval
  clearInterval: typeof clearInterval
}

// bridge.ts touches `window` only inside its functions, so stubbing it before
// the dynamic import below is enough.
const win: Win = { setInterval, clearInterval }
;(globalThis as unknown as { window: Win }).window = win

const { callBridge, isNativeBridgeAvailable } = await import('../src/lib/bridge.ts')

test('resolves when the method is attached after the call starts', async () => {
  win.pywebview = { api: {} } // object exists, method does not - the real race
  setTimeout(() => {
    win.pywebview!.api!.bridge_status = async () => ({ ok: true, running: false })
  }, 200)

  const status = await callBridge<{ ok: boolean }>('bridge_status')
  assert.equal(status.ok, true)
})

test('restores a saved theme through the same late-attach path', async () => {
  win.pywebview = { api: {} }
  setTimeout(() => {
    win.pywebview!.api!.get_config = async () => ({
      ok: true,
      config: { ui: { theme: 'dark', animations_enabled: true } },
    })
  }, 200)

  const cfg = await callBridge<{ config: { ui: { theme: string } } }>('get_config')
  assert.equal(cfg.config.ui.theme, 'dark')
})

test('still reports a genuinely missing method', async () => {
  win.pywebview = { api: { ping: async () => 'pong' } }
  await assert.rejects(callBridge('definitely_not_a_method'), /not implemented/)
})

test('still reports plain browser dev mode', async () => {
  delete win.pywebview
  assert.equal(isNativeBridgeAvailable(), false)
  await assert.rejects(callBridge('bridge_status'), /unavailable \(dev mode\)/)
})
