import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Section, Row } from '../components/Section'
import { Toggle } from '../components/Toggle'
import { Button } from '../components/Button'
import { Modal } from '../components/Modal'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { DiagBadge, type DiagState } from '../components/DiagBadge'
import { Spinner } from '../components/Spinner'
import { IconClose, IconPlus, IconTrash } from '../components/icons'
import { Segmented } from '../components/Segmented'
import { useMotionPrefs, type UiSection } from '../state/MotionPrefsContext'
import { useSpringTransition } from '../lib/motion'
import { useStrings, t, strategyGroupLabel } from '../lib/strings'
import { callBridge, isNativeBridgeAvailable, waitForBridgeReady } from '../lib/bridge'
import { isConfirmSkipped, setConfirmSkipped } from '../lib/confirmSkip'
import './SettingsScreen.css'

interface StrategyOption {
  key: string
  name: string
}
type StrategyGroups = Record<string, StrategyOption[]>

interface RewriteConfig {
  server_enabled: boolean
  server_keys: string[]
  room_id_keys: string[]
  upstream_base: string
  origin_enabled: boolean
  upstream_origin: string
  user_agent_enabled: boolean
  fallback_user_agent: string
}

interface TempDirResponse {
  ok: boolean
  error: string | null
  path: string
  resolved: string
}

interface UpdateCheckResponse {
  ok: boolean
  error: string | null
  installed: string | null
  latest: string | null
  updateAvailable: boolean
}

/** What an update did to zapret/strategies/, reported per outcome.
 *
 *  `forked` is the one that needs explaining to the user rather than just
 *  listing: it means their own edited strategy was left exactly as it was and
 *  the release's version was written beside it under a new name. */
interface StrategyChanges {
  added: string[]
  updated: string[]
  forked: [string, string][]
  skipped: [string, string][]
}

interface UpdateProgressResponse {
  ok: boolean
  error: string | null
  done: boolean
  phase: 'download' | 'extract' | 'apply' | 'done' | 'idle'
  received: number
  total: number
  applied: string[]
  version: string | null
  strategies: StrategyChanges | null
}

/** A destination preset. `kind` is NOT a mode to switch between: Ecast and
 *  Blobcast paths are disjoint, so both are always served. It only says which
 *  half of the traffic this address receives. */
type ProfileKind = 'ecast' | 'blobcast'

/** Response rewriting. Only ever applied to Ecast, which is why it lives on
 *  the profile instead of being a global section that silently applied to
 *  Blobcast traffic too. */
interface EcastSettings {
  server_enabled: boolean
  server_keys: string[]
  room_id_keys: string[]
  origin_enabled: boolean
  upstream_origin: string
  user_agent_enabled: boolean
  fallback_user_agent: string
  forward_all: boolean
  paths: string[]
}

interface BlobcastSettings {
  socketio_port: number
  intercept_session: boolean
  local_server_name: string
  log_frames: boolean
  paths: string[]
}

interface Profile {
  id: string
  name: string
  kind: ProfileKind
  upstream: string
  builtin: boolean
  ecast: EcastSettings
  blobcast: BlobcastSettings
}

/** The editable text fields of an Ecast profile, deliberately excluding the
 *  booleans so a toggle can't be handed to the text-field helper. */
type EcastField = 'server_keys' | 'room_id_keys' | 'upstream_origin' | 'fallback_user_agent'
type EcastFlag = 'server_enabled' | 'origin_enabled' | 'user_agent_enabled'

interface ProfilesConfig {
  items: Profile[]
  active_ecast: string
  active_blobcast: string
}

interface AutostartResponse {
  ok: boolean
  error: string | null
  enabled: boolean
  minimized: boolean
}

interface AppConfig {
  server: { port: number }
  zapret: { strategy: string; hide_console: boolean }
  update: { check_on_startup: boolean }
  ui?: { start_bridge_on_launch: boolean; minimize_to_tray: boolean }
  profiles: ProfilesConfig
  rewrite: RewriteConfig
}

interface ConfigResponse {
  ok: boolean
  error: string | null
  config: AppConfig | null
}

// Only the factory-reset response needs `ui` - every other call site here
// edits a single non-ui field and has no reason to touch theme/animations/
// sidebar state.
interface FullConfigResponse {
  ok: boolean
  error: string | null
  config: (AppConfig & { ui: UiSection }) | null
}

interface StrategiesResponse {
  ok: boolean
  groups: StrategyGroups
}

interface TargetResult {
  ok: boolean
  elapsedMs: number | null
  status: number | null
  error: string | null
}

/** Which protocol's hosts a result was measured against - "both" runs two
 *  full passes (see backend Api.test_strategies), so a flat results list can
 *  hold rows from either stage and this is what tells them apart. */
type TargetSet = 'ecast' | 'blobcast'

interface StrategyResult {
  key: string
  name: string
  ok: boolean
  error: string | null
  targets: Record<string, TargetResult>
  targetSet: TargetSet
}

interface TestStrategiesStart {
  ok: boolean
  error: string | null
  total: number
}

interface TestStrategiesProgress {
  ok: boolean
  error: string | null
  results: StrategyResult[]
  fastestKey: string | null
  // Which stage is running right now; null before a run starts and once it
  // finishes. Lets the popup say "Этап: Blobcast" during a "both" run.
  stage: TargetSet | null
  done: boolean
}

interface StartupUpdateCheck {
  ok: boolean
  error: string | null
  started: boolean
  done: boolean
  installed: string | null
  latest: string | null
  updateAvailable: boolean
}

interface HostlistResponse {
  ok: boolean
  error: string | null
  text: string
}

interface SaveHostlistResponse {
  ok: boolean
  error: string | null
  count: number
}

interface ConfirmRequest {
  // localStorage key for "don't ask again" - each reset button owns its own,
  // so skipping one confirmation never silences another.
  id: string
  title: string
  body: string
  confirmLabel: string
  danger?: boolean
  action: () => void
}

const STRATEGY_POLL_MS = 700

/**
 * Which top-level config sections do not take effect where they are edited.
 *
 * `bridge`: read by RuntimeCore.start(), so a running bridge keeps using the
 * values it started with - set_config only affects the NEXT start.
 * `app`: read once by main() before the window exists; nothing short of a
 * relaunch re-reads them.
 *
 * Everything absent from this map applies immediately and must NOT raise the
 * banner - `ui` (theme, animations, tray) and `paths` (read at use time) are
 * the whole of that list today. A banner that appears on a theme switch is one
 * people learn to ignore, which costs the cases where it matters.
 */
const RESTART_SCOPE: Record<string, 'bridge' | 'app'> = {
  server: 'bridge',
  zapret: 'bridge',
  profiles: 'bridge',
  proxy: 'bridge',
  rewrite: 'bridge',
  logging: 'app',
}

const splitKeys = (text: string) =>
  text
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)

export function SettingsScreen() {
  const strings = useStrings()
  const {
    animationsEnabled,
    setAnimationsEnabled,
    theme,
    setTheme,
    language,
    setLanguage,
    setSidebarCollapsed,
    setSetupComplete,
  } = useMotionPrefs()

  const [strategy, setStrategy] = useState('general')
  const [strategyGroups, setStrategyGroups] = useState<StrategyGroups>({})
  const [port, setPort] = useState('8443')
  // The last value actually confirmed by the backend (set on load and on
  // every successful persist) - not just what the input currently shows, so
  // focusing the field and blurring without typing anything can be told
  // apart from a real edit. See handlePortBlur.
  const committedPortRef = useRef('8443')
  const [hideConsole, setHideConsole] = useState(true)
  // Autostart is the one setting whose truth lives outside config.yaml - it is
  // a Windows scheduled task, and the task is what counts.
  const [autostart, setAutostart] = useState({ enabled: false, minimized: false })
  const [autostartError, setAutostartError] = useState<string | null>(null)
  const [startBridgeOnLaunch, setStartBridgeOnLaunch] = useState(false)
  const [minimizeToTray, setMinimizeToTray] = useState(true)

  const [profiles, setProfiles] = useState<ProfilesConfig | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [transferOpen, setTransferOpen] = useState(false)
  const [transferText, setTransferText] = useState('')
  const [transferReport, setTransferReport] = useState<{
    added: number
    skipped: { name: string; reason: string }[]
  } | null>(null)
  const [configError, setConfigError] = useState<string | null>(null)

  const [strategyTest, setStrategyTest] = useState<DiagState>('idle')
  const [strategyPopupOpen, setStrategyPopupOpen] = useState(false)
  const [strategyResults, setStrategyResults] = useState<StrategyResult[]>([])
  const [fastestKey, setFastestKey] = useState<string | null>(null)
  const [strategyTotal, setStrategyTotal] = useState(0)
  const [diagError, setDiagError] = useState<string | null>(null)
  // "Прочие" (Fake TLS Auto*, Simple Fake*) is skipped by default - it's the
  // slow half of the set (see diagnostics.py's HEAVY_GROUPS) - but skipping
  // it silently made the suite look like it only ever covers General and the
  // Alternatives. Opt-in, not a second default, since most runs don't need it.
  const [testHeavy, setTestHeavy] = useState(false)
  // Which protocol's hosts to probe. Ecast by default - it's the one every
  // Party Pack 7+ game needs, and it's what this test checked before
  // Blobcast pings existed at all, so a fresh install's first run stays the
  // same shape it always was.
  const [targetSet, setTargetSet] = useState<TargetSet | 'both'>('ecast')
  // Which stage is running right now, mirrored from progress.stage - drives
  // the "Этап: Ecast/Blobcast" label so a "both" run doesn't look stuck
  // during the (much longer) second half.
  const [runningStage, setRunningStage] = useState<TargetSet | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const pollRef = useRef<number | undefined>(undefined)

  const [tempDir, setTempDir] = useState<TempDirResponse | null>(null)
  const [checkOnStartup, setCheckOnStartup] = useState(false)
  const [updateCheck, setUpdateCheck] = useState<DiagState>('idle')
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResponse | null>(null)
  const [updateOpen, setUpdateOpen] = useState(false)
  const [updateProgress, setUpdateProgress] = useState<UpdateProgressResponse | null>(null)
  const updatePollRef = useRef<number | undefined>(undefined)

  const [hostlistOpen, setHostlistOpen] = useState(false)
  const [hostlist, setHostlist] = useState('')
  const [hostlistError, setHostlistError] = useState<string | null>(null)

  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null)
  // Which restart the settings changed so far are waiting on, if any. See
  // RESTART_SCOPE for why this is not simply "something changed".
  const [pendingRestart, setPendingRestart] = useState<'bridge' | 'app' | null>(null)
  const [restarting, setRestarting] = useState(false)
  const transition = useSpringTransition('default')

  // Leaving the screen mid-run must not keep polling a dead component.
  useEffect(() => () => stopPolling(), [])
  useEffect(() => () => stopUpdatePolling(), [])

  useEffect(() => {
    waitForBridgeReady().then(() => {
      if (!isNativeBridgeAvailable()) return
      callBridge<ConfigResponse>('get_config').then((result) => {
        if (result.ok && result.config) applyConfig(result.config)
      })
      callBridge<StrategiesResponse>('list_strategies').then((result) => {
        if (result.ok) setStrategyGroups(result.groups)
      })
      callBridge<TempDirResponse>('get_temp_dir').then((result) => {
        if (result.ok) setTempDir(result)
      })
      // Asked separately from get_config on purpose: the scheduled task is the
      // source of truth here, and it can have been removed outside the app.
      callBridge<AutostartResponse>('get_autostart')
        .then((result) => {
          if (result.ok) setAutostart({ enabled: result.enabled, minimized: result.minimized })
        })
        .catch(() => {})
    })
  }, [])

  // "Проверять при запуске": main() fires the check before the window even
  // opens (see Api.start_startup_update_check), so by the time this screen
  // mounts it's often still in flight. Polled rather than fetched once,
  // because "not done yet" and "the setting is off" both look like "nothing
  // to show" from a single call - only polling tells them apart. Capped at
  // 20 tries (~20s): GitHub is routinely unreachable from the networks this
  // app exists for, and giving up quietly is correct for a background check
  // that has "Проверить обновления" right below it for anyone who wants an
  // explicit answer.
  useEffect(() => {
    let cancelled = false
    let attempts = 0

    async function poll() {
      if (cancelled || !isNativeBridgeAvailable()) return
      const result = await callBridge<StartupUpdateCheck>('startup_update_check')
      if (cancelled || !result.started) return
      if (!result.done) {
        attempts += 1
        if (attempts < 20) window.setTimeout(poll, 1000)
        return
      }
      setUpdateInfo(result)
      setUpdateCheck(result.ok ? 'done' : 'error')
    }

    waitForBridgeReady().then(poll)
    return () => {
      cancelled = true
    }
  }, [])

  function applyConfig(config: AppConfig) {
    setStrategy(config.zapret.strategy)
    setPort(String(config.server.port))
    committedPortRef.current = String(config.server.port)
    setHideConsole(config.zapret.hide_console)
    setCheckOnStartup(config.update.check_on_startup)
    setProfiles(config.profiles)
    if (config.ui) {
      setStartBridgeOnLaunch(config.ui.start_bridge_on_launch)
      setMinimizeToTray(config.ui.minimize_to_tray)
    }
  }

  async function noteChange(patch: Record<string, unknown>) {
    const scopes = Object.keys(patch)
      .map((key) => RESTART_SCOPE[key])
      .filter(Boolean)
    if (scopes.length === 0) return

    if (scopes.includes('app')) {
      // App beats bridge: relaunching restarts the bridge too, so offering the
      // narrower action while the wider one is pending would leave half the
      // change unapplied with the banner gone. Shown regardless of whether
      // the bridge happens to be running - an app-scoped setting (logging)
      // needs a relaunch either way.
      setPendingRestart('app')
      return
    }

    // 'bridge' scope only from here. Restarting a bridge that isn't running
    // is a no-op offer: nothing is currently using the old value, and the
    // next ordinary start already reads the new config on its own (see
    // applyPendingRestart's docstring). Checked live rather than trusted from
    // stale state - every screen stays mounted (see App.tsx), so the bridge
    // can have been toggled from Home while this one sat in the background.
    if (!isNativeBridgeAvailable()) return
    try {
      const status = await callBridge<{ running: boolean }>('bridge_status')
      if (!status.running) return
      setPendingRestart((current) => (current === 'app' ? 'app' : 'bridge'))
    } catch {
      // Unknown bridge state is not a reason to nag about a restart.
    }
  }

  function persist(patch: Record<string, unknown>) {
    noteChange(patch)
    if (!isNativeBridgeAvailable()) return
    callBridge('update_config', patch).catch(() => {})
  }

  /**
   * Autostart does not go through persist(): creating or deleting a Windows
   * scheduled task can be refused (no rights, schtasks missing, policy), and
   * the toggle must then snap back to what Windows really has rather than
   * showing what the user wanted. set_autostart writes the config itself.
   */
  async function changeAutostart(enabled: boolean, minimized: boolean) {
    setAutostart({ enabled, minimized })
    setAutostartError(null)
    if (!isNativeBridgeAvailable()) return
    try {
      const result = await callBridge<AutostartResponse>('set_autostart', enabled, minimized)
      setAutostart({ enabled: result.enabled, minimized: result.minimized })
      setAutostartError(result.ok ? null : result.error)
    } catch (err) {
      setAutostart({ enabled: false, minimized })
      setAutostartError(String(err))
    }
  }

  /**
   * For fields the backend validates (upstream URLs, JSON key names). The
   * response carries the resulting config either way - on rejection it echoes
   * the previous one, so re-applying it snaps the input back to the last good
   * value while the error explains why. No duplicated validation in TS.
   */
  async function persistChecked(patch: Record<string, unknown>) {
    if (!isNativeBridgeAvailable()) return
    try {
      const result = await callBridge<ConfigResponse>('update_config', patch)
      if (result.config) applyConfig(result.config)
      setConfigError(result.ok ? null : result.error)
      // Only on acceptance, unlike persist(): a rejected patch echoes the
      // PREVIOUS config back, so nothing changed and there is nothing to
      // restart for.
      if (result.ok) noteChange(patch)
    } catch (err) {
      setConfigError(String(err))
    }
  }

  /**
   * Apply what the banner is waiting on.
   *
   * The bridge case is a stop/start rather than a dedicated backend call:
   * RuntimeCore reads the config in start(), so going down and back up IS the
   * apply step, and there is no second code path to keep correct. A bridge
   * that was not running needs neither - the next start will pick the new
   * values up on its own, so the banner just clears.
   */
  async function applyPendingRestart() {
    const scope = pendingRestart
    if (scope === null || !isNativeBridgeAvailable()) {
      setPendingRestart(null)
      return
    }
    setRestarting(true)
    try {
      if (scope === 'app') {
        const result = await callBridge<{ ok: boolean; error: string | null }>('restart_app')
        if (!result.ok) {
          setConfigError(result.error)
          return
        }
        return // the window is going away; leave the banner where it is
      }
      const status = await callBridge<{ running: boolean }>('bridge_status')
      if (status.running) {
        await callBridge('bridge_stop')
        const started = await callBridge<{ ok: boolean; error: string | null }>('bridge_start')
        if (!started.ok) {
          setConfigError(started.error)
          return
        }
      }
      setPendingRestart(null)
    } catch (err) {
      setConfigError(String(err))
    } finally {
      setRestarting(false)
    }
  }

  function handleStrategyChange(key: string) {
    setStrategy(key)
    persist({ zapret: { strategy: key } })
  }

  function handleHideConsoleChange(value: boolean) {
    setHideConsole(value)
    persist({ zapret: { hide_console: value } })
  }

  function handlePortBlur() {
    const value = Number(port)
    if (!Number.isFinite(value) || value <= 0) return
    if (String(value) === committedPortRef.current) return
    persistChecked({ server: { port: value } })
  }

  // null tells _deep_merge to drop the key so pydantic refills its default -
  // the frontend never has to know what that default is.
  const resetPort = () => persistChecked({ server: { port: null } })

  // Every section, same null-unset trick - a full factory reset is just
  // "reset" applied to the whole document instead of one part of it. The
  // hostlist is a separate file zapret reads directly, not a Config field,
  // so it deliberately survives this untouched (the confirm body says so).
  async function factoryReset() {
    if (!isNativeBridgeAvailable()) return
    const result = await callBridge<FullConfigResponse>('update_config', {
      server: null,
      zapret: null,
      logging: null,
      ui: null,
      paths: null,
      update: null,
      proxy: null,
      profiles: null,
      rewrite: null,
    })
    if (!result.ok) {
      setConfigError(result.error)
      return
    }

    // Restart rather than re-seeding React state in place.
    //
    // `ui: null` also drops ui.setup_complete, so the reset genuinely re-arms
    // the first-run wizard - but nothing re-read that, so the app carried on
    // looking configured and the wizard ambushed the user at the next launch
    // instead. A relaunch is what "заводские настройки" has to mean here
    // anyway: the port, the cert dir and the log level are all read once at
    // startup, so half the defaults this just restored would not have taken
    // effect in this process regardless.
    const restarted = await callBridge<{ ok: boolean; error: string | null }>('restart_app')
    if (restarted.ok) return

    // Relaunch refused (it can fail on its own - see Api.restart_app). Fall
    // back to applying the reset in place, so the app at least reflects it
    // rather than showing the pre-reset values as if nothing happened.
    if (result.config) {
      applyConfig(result.config)
      // MotionPrefsContext owns these in its own state, seeded once on mount -
      // it has no way to know this reset just changed them underneath it. Its
      // setters re-persist the value they were just given, which is a
      // redundant round trip but not an incorrect one.
      setTheme(result.config.ui.theme)
      setAnimationsEnabled(result.config.ui.animations_enabled)
      setSidebarCollapsed(result.config.ui.sidebar_collapsed)
      setSetupComplete(result.config.ui.setup_complete)
    }
    setConfigError(restarted.error)
  }

  function confirmThenRun(request: ConfirmRequest) {
    if (isConfirmSkipped(request.id)) {
      request.action()
      return
    }
    setConfirmRequest(request)
  }

  const activeIdFor = (kind: ProfileKind) =>
    kind === 'ecast' ? profiles?.active_ecast : profiles?.active_blobcast

  const profilesOfKind = (kind: ProfileKind) =>
    profiles?.items.filter((p) => p.kind === kind) ?? []

  // Which profile the panel is EDITING - deliberately not the same question
  // as which one is in use. There can be several profiles of a kind and only
  // one is active, so editing an inactive one has to be possible; conflating
  // the two would mean a profile could only be configured by first switching
  // the running bridge onto it.
  const edited =
    profiles?.items.find((p) => p.id === editingId) ?? profiles?.items[0] ?? null

  /** Writes the whole items array: _deep_merge replaces lists rather than
   *  merging them, so a partial patch here would drop every other profile. */
  function patchProfile(id: string, changes: Partial<Profile>) {
    if (!profiles) return
    persistChecked({
      profiles: { items: profiles.items.map((p) => (p.id === id ? { ...p, ...changes } : p)) },
    })
  }

  function patchEcast(profile: Profile, changes: Partial<EcastSettings>) {
    patchProfile(profile.id, { ecast: { ...profile.ecast, ...changes } })
  }

  function activateProfile(profile: Profile) {
    const key = profile.kind === 'ecast' ? 'active_ecast' : 'active_blobcast'
    persistChecked({ profiles: { [key]: profile.id } })
  }

  /**
   * "+" creates a profile and opens it. Its address starts as the official
   * one for its kind rather than empty: an empty upstream fails validation,
   * so a blank field would greet the user with an error they did nothing to
   * cause. Everything else is schema defaults, which is what "empty" means
   * here.
   */
  async function addProfile() {
    if (!profiles) return
    const template = profiles.items.find((p) => p.kind === 'ecast' && p.builtin)
    if (!template) return
    const id = `custom-${Date.now()}`
    await persistChecked({
      profiles: {
        items: [
          ...profiles.items,
          {
            ...template,
            id,
            name: strings.settings.profilesNewName,
            kind: 'ecast' as ProfileKind,
            builtin: false,
          },
        ],
      },
    })
    setEditingId(id)
  }

  /**
   * Changing an existing profile's kind. Both settings blocks live on every
   * profile, so this is lossless in both directions - switch to Blobcast and
   * back and the Ecast rewriting is still there.
   *
   * If it was the active profile of its old kind, that kind is left pointing
   * at something that is no longer of that kind. The backend falls back to
   * the built-in when resolving, but the id would stay dangling in the
   * config, so it is repointed here in the same write.
   */
  function changeKind(profile: Profile, kind: ProfileKind) {
    if (!profiles || profile.builtin || profile.kind === kind) return
    const patch: Record<string, unknown> = {
      items: profiles.items.map((p) => (p.id === profile.id ? { ...p, kind } : p)),
    }
    if (activeIdFor(profile.kind) === profile.id) {
      const key = profile.kind === 'ecast' ? 'active_ecast' : 'active_blobcast'
      patch[key] = profilesOfKind(profile.kind).find((p) => p.builtin)?.id
    }
    persistChecked({ profiles: patch })
  }

  async function openTransfer() {
    setTransferReport(null)
    setTransferOpen(true)
    if (!isNativeBridgeAvailable()) return
    const result = await callBridge<{ ok: boolean; error: string | null; json: string }>(
      'export_profiles',
    )
    if (result.ok) setTransferText(result.json)
    else setConfigError(result.error)
  }

  async function runTransfer(method: string, ...args: unknown[]) {
    if (!isNativeBridgeAvailable()) return
    const result = await callBridge<{
      ok: boolean
      error: string | null
      config: AppConfig | null
      report: { added: number; skipped: { name: string; reason: string }[] } | null
      json?: string
    }>(method, ...args)
    if (!result.ok) return setConfigError(result.error)
    setConfigError(null)
    if (result.config) applyConfig(result.config)
    if (result.report) setTransferReport(result.report)
    if (result.json) setTransferText(result.json)
  }

  function deleteProfile(profile: Profile) {
    if (!profiles) return
    // If the deleted one was selected, point the kind back at its built-in in
    // the same write. The backend already falls back when resolving, but a
    // dangling id would leave the <select> showing nothing at all.
    const patch: Record<string, unknown> = {
      items: profiles.items.filter((p) => p.id !== profile.id),
    }
    if (activeIdFor(profile.kind) === profile.id) {
      const key = profile.kind === 'ecast' ? 'active_ecast' : 'active_blobcast'
      patch[key] = profilesOfKind(profile.kind).find((p) => p.builtin)?.id
    }
    persistChecked({ profiles: patch })
  }

  async function pickTempDir() {
    if (!isNativeBridgeAvailable()) return
    const result = await callBridge<{ ok: boolean; error: string | null }>('pick_temp_dir')
    if (!result.ok) return setConfigError(result.error)
    setConfigError(null)
    // Re-read rather than trust the returned path: the backend resolves an
    // empty/relative value, and the resolved form is what the row shows.
    const refreshed = await callBridge<TempDirResponse>('get_temp_dir')
    if (refreshed.ok) setTempDir(refreshed)
  }

  async function runUpdateCheck() {
    setUpdateCheck('running')
    setUpdateInfo(null)
    if (!isNativeBridgeAvailable()) return setUpdateCheck('idle')
    const result = await callBridge<UpdateCheckResponse>('check_zapret_update')
    setUpdateInfo(result)
    setUpdateCheck(result.ok ? 'done' : 'error')
  }

  function stopUpdatePolling() {
    if (updatePollRef.current !== undefined) {
      window.clearInterval(updatePollRef.current)
      updatePollRef.current = undefined
    }
  }

  async function startUpdate() {
    setUpdateOpen(true)
    setUpdateProgress(null)
    if (!isNativeBridgeAvailable()) return
    const started = await callBridge<{ ok: boolean; error: string | null }>(
      'start_zapret_update',
    )
    if (!started.ok) {
      setUpdateProgress({
        ok: false, error: started.error, done: true, phase: 'idle',
        received: 0, total: 0, applied: [], version: null, strategies: null,
      })
      return
    }
    stopUpdatePolling()
    updatePollRef.current = window.setInterval(async () => {
      try {
        const progress = await callBridge<UpdateProgressResponse>('zapret_update_progress')
        setUpdateProgress(progress)
        if (progress.done) stopUpdatePolling()
      } catch (err) {
        stopUpdatePolling()
        setUpdateProgress({
          ok: false, error: String(err), done: true, phase: 'idle',
          received: 0, total: 0, applied: [], version: null, strategies: null,
        })
      }
    }, STRATEGY_POLL_MS)
  }

  function closeUpdate() {
    stopUpdatePolling()
    // Only cancel work that is still in flight - cancelling a finished
    // update would be a no-op, but asking is a pointless round trip.
    if (isNativeBridgeAvailable() && updateProgress && !updateProgress.done) {
      callBridge('cancel_zapret_update').catch(() => {})
    }
    setUpdateOpen(false)
  }

  async function openHostlist() {
    setHostlistError(null)
    setHostlistOpen(true)
    if (!isNativeBridgeAvailable()) return
    const result = await callBridge<HostlistResponse>('get_hostlist')
    if (result.ok) setHostlist(result.text)
    else setHostlistError(result.error)
  }

  async function saveHostlist() {
    if (!isNativeBridgeAvailable()) return setHostlistOpen(false)
    const result = await callBridge<SaveHostlistResponse>('save_hostlist', hostlist)
    // The backend names the offending line; showing it beats closing the
    // modal and losing the edit the user has to redo.
    if (result.ok) setHostlistOpen(false)
    else setHostlistError(result.error)
  }

  function stopPolling() {
    if (pollRef.current !== undefined) {
      window.clearInterval(pollRef.current)
      pollRef.current = undefined
    }
  }

  async function runStrategyTest() {
    setStrategyTest('running')
    setDiagError(null)
    setExportError(null)
    setStrategyResults([])
    setFastestKey(null)
    setStrategyTotal(0)
    setRunningStage(targetSet === 'blobcast' ? 'blobcast' : 'ecast')
    setStrategyPopupOpen(true)
    if (!isNativeBridgeAvailable()) {
      window.setTimeout(() => setStrategyTest('done'), 1200)
      return
    }

    // The suite runs for minutes, so it's a background job on the backend:
    // start it, then poll. Waiting on one long call is what previously let a
    // timeout throw away every result that had already been measured.
    const started = await callBridge<TestStrategiesStart>('test_strategies', !testHeavy, targetSet)
    if (!started.ok) {
      setStrategyTest('error')
      setDiagError(started.error)
      return
    }
    setStrategyTotal(started.total)

    stopPolling()
    pollRef.current = window.setInterval(async () => {
      try {
        const progress = await callBridge<TestStrategiesProgress>('test_strategies_progress')
        setStrategyResults(progress.results ?? [])
        setFastestKey(progress.fastestKey ?? null)
        if (progress.stage) setRunningStage(progress.stage)
        if (progress.done) {
          stopPolling()
          setRunningStage(null)
          setStrategyTest(progress.error ? 'error' : 'done')
          setDiagError(progress.error)
        }
      } catch (err) {
        stopPolling()
        setRunningStage(null)
        setStrategyTest('error')
        setDiagError(String(err))
      }
    }, STRATEGY_POLL_MS)
  }

  async function exportStrategyResults(fmt: 'json' | 'html') {
    setExportError(null)
    if (!isNativeBridgeAvailable()) return
    const result = await callBridge<{ ok: boolean; error: string | null; path: string }>(
      'export_strategy_results',
      fmt,
    )
    if (!result.ok) setExportError(result.error)
  }

  function applyStrategy(key: string) {
    handleStrategyChange(key)
    closeStrategyPopup()
  }

  function closeStrategyPopup() {
    stopPolling()
    if (isNativeBridgeAvailable()) {
      // Without this the backend keeps cycling Zapret through every remaining
      // strategy after the popup is gone, leaving winws.exe running on
      // whichever one it reached. Cancel also restores the configured strategy.
      callBridge('test_strategies_cancel').catch(() => {})
    }
    setStrategyPopupOpen(false)
    setStrategyTest('idle')
    setRunningStage(null)
    setExportError(null)
  }

  /** One rewrite row, scoped to the profile being edited. Uncontrolled with
   *  a per-profile `key`, so switching profiles reloads the values instead of
   *  needing a draft map keyed by profile AND field. */
  function ecastRow(
    profile: Profile,
    field: EcastField,
    label: string,
    hint: string,
    flag?: EcastFlag,
  ) {
    const on = flag ? profile.ecast[flag] : true
    const isList = field === 'server_keys' || field === 'room_id_keys'
    const current = profile.ecast[field]
    return (
      <Row
        key={field}
        label={label}
        hint={hint}
        control={
          <div className="bb-field-row">
            {flag && (
              <Toggle
                checked={on}
                label={label}
                onChange={(v) => patchEcast(profile, { [flag]: v })}
              />
            )}
            <input
              key={`${field}-${profile.id}`}
              className="bb-input bb-input--wide text-mono"
              disabled={!on}
              spellCheck={false}
              defaultValue={Array.isArray(current) ? current.join(', ') : current}
              onBlur={(e) => {
                const value = isList ? splitKeys(e.target.value) : e.target.value.trim()
                if (JSON.stringify(value) === JSON.stringify(current)) return
                patchEcast(profile, { [field]: value })
              }}
            />
          </div>
        }
      />
    )
  }

  function ecastRows(profile: Profile) {
    return (
      <>
        <Row
          label={strings.settings.profilesEcastForwardAllLabel}
          hint={strings.settings.profilesEcastForwardAllHint}
          control={
            <Toggle
              checked={profile.ecast.forward_all}
              label={strings.settings.profilesEcastForwardAllLabel}
              onChange={(v) => patchEcast(profile, { forward_all: v })}
            />
          }
        />
        <Row
          label={strings.settings.profilesEcastPathsLabel}
          hint={strings.settings.profilesEcastPathsHint}
          control={
            <input
              key={`ecast-paths-${profile.id}`}
              className="bb-input bb-input--wide text-mono"
              // Only meaningful with "весь трафик" off - disabled rather than
              // hidden so the current list stays readable either way.
              disabled={profile.ecast.forward_all}
              defaultValue={profile.ecast.paths.join(', ')}
              spellCheck={false}
              onBlur={(e) => {
                const paths = splitKeys(e.target.value)
                if (JSON.stringify(paths) === JSON.stringify(profile.ecast.paths)) return
                patchEcast(profile, { paths })
              }}
            />
          }
        />

        <div className="bb-subheading">
          <span className="text-subtitle">{strings.settings.profilesEcastSettingsTitle}</span>
          <span className="text-caption">{strings.settings.profilesEcastSettingsHint}</span>
        </div>
        {ecastRow(
          profile,
          'server_keys',
          strings.settings.serverKeysLabel,
          strings.settings.serverKeysHint,
          'server_enabled',
        )}
        {ecastRow(
          profile,
          'room_id_keys',
          strings.settings.roomIdKeysLabel,
          strings.settings.roomIdKeysHint,
        )}
        {ecastRow(
          profile,
          'upstream_origin',
          strings.settings.upstreamOriginLabel,
          strings.settings.upstreamOriginHint,
          'origin_enabled',
        )}
        {ecastRow(
          profile,
          'fallback_user_agent',
          strings.settings.userAgentLabel,
          strings.settings.userAgentHint,
          'user_agent_enabled',
        )}
      </>
    )
  }

  function patchBlobcast(profile: Profile, changes: Partial<BlobcastSettings>) {
    patchProfile(profile.id, { blobcast: { ...profile.blobcast, ...changes } })
  }

  function blobcastRows(profile: Profile) {
    const b = profile.blobcast
    return (
      <>
        <div className="bb-subheading">
          <span className="text-subtitle">{strings.settings.profilesBlobcastSettingsTitle}</span>
        </div>
        <Row
          label={strings.settings.profilesInterceptLabel}
          hint={strings.settings.profilesInterceptHint}
          control={
            <Toggle
              checked={b.intercept_session}
              label={strings.settings.profilesInterceptLabel}
              onChange={(v) => patchBlobcast(profile, { intercept_session: v })}
            />
          }
        />
        <Row
          label={strings.settings.profilesLocalNameLabel}
          hint={strings.settings.profilesLocalNameHint}
          control={
            <input
              key={`local-${profile.id}`}
              className="bb-input bb-input--wide text-mono"
              defaultValue={b.local_server_name}
              disabled={!b.intercept_session}
              spellCheck={false}
              onBlur={(e) => {
                const name = e.target.value.trim()
                if (!name || name === b.local_server_name) return
                patchBlobcast(profile, { local_server_name: name })
              }}
            />
          }
        />
        <Row
          label={strings.settings.profilesSocketioPortLabel}
          hint={strings.settings.profilesSocketioPortHint}
          control={
            <input
              key={`port-${profile.id}`}
              className="bb-input bb-input--narrow text-mono"
              defaultValue={String(b.socketio_port)}
              inputMode="numeric"
              onBlur={(e) => {
                const port = Number(e.target.value.replace(/\D/g, ''))
                if (!port || port === b.socketio_port) return
                patchBlobcast(profile, { socketio_port: port })
              }}
            />
          }
        />
        <Row
          label={strings.settings.profilesLogFramesLabel}
          hint={strings.settings.profilesLogFramesHint}
          control={
            <Toggle
              checked={b.log_frames}
              label={strings.settings.profilesLogFramesLabel}
              onChange={(v) => patchBlobcast(profile, { log_frames: v })}
            />
          }
        />
        <Row
          label={strings.settings.profilesBlobcastPathsLabel}
          hint={strings.settings.profilesBlobcastPathsHint}
          control={
            <input
              key={`paths-${profile.id}`}
              className="bb-input bb-input--wide text-mono"
              defaultValue={b.paths.join(', ')}
              spellCheck={false}
              onBlur={(e) => {
                const paths = splitKeys(e.target.value)
                if (JSON.stringify(paths) === JSON.stringify(b.paths)) return
                patchBlobcast(profile, { paths })
              }}
            />
          }
        />
      </>
    )
  }

  return (
    <div>
      <h1 className="text-display" style={{ marginBottom: 'var(--space-6)' }}>
        {strings.settings.title}
      </h1>

      <Section title={strings.settings.systemSectionTitle}>
        <Row
          label={strings.settings.languageLabel}
          hint={strings.settings.languageHint}
          control={
            <Segmented
              value={language}
              onChange={setLanguage}
              ariaLabel={strings.settings.languageLabel}
              options={[
                { value: 'system', label: strings.settings.languageSystem },
                { value: 'ru', label: 'RU' },
                { value: 'en', label: 'EN' },
              ]}
            />
          }
        />
        <Row
          label={strings.settings.darkThemeLabel}
          control={
            <Toggle checked={theme === 'dark'} onChange={(v) => setTheme(v ? 'dark' : 'light')} />
          }
        />
        <Row
          label={strings.settings.animationsLabel}
          control={<Toggle checked={animationsEnabled} onChange={setAnimationsEnabled} />}
        />
        {/* Reflects what Windows actually has, not what config.yaml believes -
            the task can be deleted in Task Scheduler behind our back, so
            get_autostart reads the task itself. */}
        <Row
          label={strings.settings.autostartLabel}
          hint={strings.settings.autostartHint}
          control={
            <Toggle
              checked={autostart.enabled}
              onChange={(v) => changeAutostart(v, autostart.minimized)}
            />
          }
        />
        {autostart.enabled && (
          <Row
            label={strings.settings.autostartMinimizedLabel}
            hint={strings.settings.autostartMinimizedHint}
            control={
              <Toggle
                checked={autostart.minimized}
                onChange={(v) => changeAutostart(true, v)}
              />
            }
          />
        )}
        <Row
          label={strings.settings.startBridgeOnLaunchLabel}
          hint={strings.settings.startBridgeOnLaunchHint}
          control={
            <Toggle
              checked={startBridgeOnLaunch}
              onChange={(v) => {
                setStartBridgeOnLaunch(v)
                persist({ ui: { start_bridge_on_launch: v } })
              }}
            />
          }
        />
        <Row
          label={strings.settings.minimizeToTrayLabel}
          hint={strings.settings.minimizeToTrayHint}
          control={
            <Toggle
              checked={minimizeToTray}
              onChange={(v) => {
                setMinimizeToTray(v)
                persist({ ui: { minimize_to_tray: v } })
              }}
            />
          }
        />
        {autostartError && <p className="text-caption bb-diag__error">{autostartError}</p>}
        {/* Below the startup group, with the port and the temp folder: these
            three are the machinery, the four above are about when and how the
            app shows up. */}
        <Row
          label={strings.settings.hideConsoleLabel}
          hint={strings.settings.hideConsoleHint}
          control={<Toggle checked={hideConsole} onChange={handleHideConsoleChange} />}
        />
        <Row
          label={strings.settings.portLabel}
          hint={strings.settings.portHint}
          control={
            <div className="bb-field-row">
              <input
                className="bb-input bb-input--narrow text-mono"
                value={port}
                onChange={(e) => setPort(e.target.value.replace(/\D/g, ''))}
                onBlur={handlePortBlur}
                inputMode="numeric"
                aria-label={strings.settings.portAriaLabel}
              />
              <Button
                variant="ghost"
                onClick={() =>
                  confirmThenRun({
                    id: 'reset-port',
                    title: strings.settings.resetPortConfirmTitle,
                    body: strings.settings.resetPortConfirmBody,
                    confirmLabel: strings.settings.resetPortButton,
                    action: resetPort,
                  })
                }
              >
                {strings.settings.resetPortButton}
              </Button>
            </div>
          }
        />
        <Row
          label={strings.settings.tempDirLabel}
          hint={strings.settings.tempDirHint}
          control={
            <div className="bb-field-row">
              <input
                className="bb-input bb-input--wide text-mono"
                readOnly
                value={tempDir?.resolved ?? ''}
                title={tempDir?.resolved ?? ''}
              />
              <Button variant="secondary" onClick={pickTempDir}>
                {strings.settings.tempDirButton}
              </Button>
            </div>
          }
        />
        <Row
          label={strings.settings.factoryResetLabel}
          hint={strings.settings.factoryResetHint}
          control={
            <Button
              variant="danger"
              onClick={() =>
                confirmThenRun({
                  id: 'factory-reset',
                  title: strings.settings.factoryResetConfirmTitle,
                  body: strings.settings.factoryResetConfirmBody,
                  confirmLabel: strings.settings.factoryResetButton,
                  danger: true,
                  action: factoryReset,
                })
              }
            >
              {strings.settings.factoryResetButton}
            </Button>
          }
        />
      </Section>

      <Section title={strings.settings.networkSectionTitle} description={strings.settings.networkSectionDescription}>
        <Row
          label={strings.settings.strategyLabel}
          hint={strings.settings.strategyHint}
          control={
            <select
              className="bb-select"
              value={strategy}
              onChange={(e) => handleStrategyChange(e.target.value)}
            >
              {Object.keys(strategyGroups)
                .filter((group) => strategyGroups[group]?.length)
                .map((group) => (
                  <optgroup key={group} label={strategyGroupLabel(group, strings)}>
                    {strategyGroups[group].map((opt) => (
                      <option key={opt.key} value={opt.key}>
                        {opt.name}
                      </option>
                    ))}
                  </optgroup>
                ))}
            </select>
          }
        />
        {/* Right under the strategy picker: the hostlist is what the chosen
            strategy is pointed at, so the two only make sense together. It
            used to sit below the test controls, three rows away. */}
        <Row
          label={strings.settings.hostlistLabel}
          hint={strings.settings.hostlistHint}
          control={
            <Button variant="secondary" onClick={openHostlist}>
              {strings.settings.hostlistButton}
            </Button>
          }
        />
        <Row
          label={strings.settings.strategyTargetSetLabel}
          hint={strings.settings.strategyTargetSetHint}
          control={
            <Segmented<TargetSet | 'both'>
              value={targetSet}
              disabled={strategyTest === 'running'}
              ariaLabel={strings.settings.strategyTargetSetLabel}
              options={[
                { value: 'ecast', label: strings.settings.profilesKindEcast },
                { value: 'blobcast', label: strings.settings.profilesKindBlobcast },
                { value: 'both', label: strings.settings.strategyTargetSetBoth },
              ]}
              onChange={setTargetSet}
            />
          }
        />
        <Row
          label={strings.settings.strategyTestLabel}
          hint={strings.settings.strategyTestHint}
          control={
            <div className="bb-diag">
              <Button
                variant="secondary"
                onClick={runStrategyTest}
                disabled={strategyTest === 'running'}
              >
                {strategyTest === 'running'
                  ? strings.settings.strategyTestButtonRunning
                  : strings.settings.strategyTestButtonIdle}
              </Button>
              <DiagBadge state={strategyTest} />
            </div>
          }
        />
        <Row
          label={strings.settings.testHeavyLabel}
          hint={strings.settings.testHeavyHint}
          control={<Toggle checked={testHeavy} onChange={setTestHeavy} />}
        />
        {diagError && !strategyPopupOpen && (
          <p className="text-caption bb-diag__error">{diagError}</p>
        )}
      </Section>

      <Section
        title={strings.settings.updateSectionTitle}
        description={strings.settings.updateSectionDescription}
      >
        <Row
          label={strings.settings.updateVersionLabel}
          control={
            <span className="text-mono">{updateInfo?.installed ?? '—'}</span>
          }
        />
        <Row
          label={strings.settings.updateCheckOnStartupLabel}
          hint={strings.settings.updateCheckOnStartupHint}
          control={
            <Toggle
              checked={checkOnStartup}
              onChange={(v) => {
                setCheckOnStartup(v)
                persist({ update: { check_on_startup: v } })
              }}
            />
          }
        />
        <Row
          label={strings.settings.updateCheckLabel}
          hint={strings.settings.updateCheckHint}
          control={
            <div className="bb-diag">
              <Button
                variant="secondary"
                onClick={runUpdateCheck}
                disabled={updateCheck === 'running'}
              >
                {updateCheck === 'running'
                  ? strings.settings.updateCheckButtonRunning
                  : strings.settings.updateCheckButtonIdle}
              </Button>
              <DiagBadge state={updateCheck} />
              {updateInfo?.ok && updateInfo.updateAvailable && (
                <Button
                  variant="primary"
                  onClick={() =>
                    confirmThenRun({
                      id: 'zapret-update',
                      title: strings.settings.updateConfirmTitle,
                      body: strings.settings.updateConfirmBody,
                      confirmLabel: strings.settings.updateStartButton,
                      action: startUpdate,
                    })
                  }
                >
                  {strings.settings.updateStartButton}
                </Button>
              )}
            </div>
          }
        />
        {updateInfo?.ok && !updateInfo.updateAvailable && updateInfo.latest && (
          <p className="text-caption">
            {t(strings.settings.updateUpToDate, { version: updateInfo.latest })}
          </p>
        )}
        {updateInfo?.ok && updateInfo.updateAvailable && (
          <p className="text-caption">
            {t(strings.settings.updateAvailable, {
              latest: updateInfo.latest ?? '?',
              installed: updateInfo.installed ?? '?',
            })}
          </p>
        )}
        {updateInfo && !updateInfo.ok && (
          <p className="text-caption bb-diag__error">{updateInfo.error}</p>
        )}
      </Section>

      <Section
        title={strings.settings.profilesSectionTitle}
        description={strings.settings.profilesSectionDescription}
      >
        <Row
          label={strings.settings.profilesPickerLabel}
          hint={strings.settings.profilesPickerHint}
          control={
            <div className="bb-field-row">
              <select
                className="bb-input bb-input--wide"
                value={edited?.id ?? ''}
                onChange={(e) => setEditingId(e.target.value)}
              >
                {(['ecast', 'blobcast'] as ProfileKind[]).map((kind) => (
                  <optgroup
                    key={kind}
                    label={
                      kind === 'ecast'
                        ? `${strings.settings.profilesKindEcast} — ${strings.settings.profilesKindEcastHint}`
                        : `${strings.settings.profilesKindBlobcast} — ${strings.settings.profilesKindBlobcastHint}`
                    }
                  >
                    {profilesOfKind(kind).map((profile) => (
                      <option key={profile.id} value={profile.id}>
                        {profile.name}
                        {activeIdFor(kind) === profile.id
                          ? ` (${strings.settings.profilesActiveBadge})`
                          : ''}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <Button
                variant="ghost"
                onClick={addProfile}
                ariaLabel={strings.settings.profilesAddAriaLabel}
                title={strings.settings.profilesAddAriaLabel}
              >
                <IconPlus />
              </Button>
            </div>
          }
        />

        {edited && (
          <>
            <Row
              label={strings.settings.profilesKindLabel}
              hint={strings.settings.profilesKindHint}
              control={
                <Segmented<ProfileKind>
                  value={edited.kind}
                  disabled={edited.builtin}
                  ariaLabel={strings.settings.profilesKindLabel}
                  options={[
                    { value: 'ecast', label: strings.settings.profilesKindEcast },
                    { value: 'blobcast', label: strings.settings.profilesKindBlobcast },
                  ]}
                  onChange={(kind) => changeKind(edited, kind)}
                />
              }
            />
            <Row
              label={strings.settings.profilesNameLabel}
              control={
                <input
                  key={`name-${edited.id}`}
                  className="bb-input bb-input--wide"
                  defaultValue={edited.name}
                  disabled={edited.builtin}
                  onBlur={(e) => {
                    const name = e.target.value.trim()
                    if (name && name !== edited.name) patchProfile(edited.id, { name })
                  }}
                />
              }
            />
            <Row
              label={strings.settings.profilesUpstreamLabel}
              hint={strings.settings.profilesUpstreamHint}
              control={
                <input
                  key={`upstream-${edited.id}`}
                  className="bb-input bb-input--wide text-mono"
                  defaultValue={edited.upstream}
                  disabled={edited.builtin}
                  spellCheck={false}
                  onBlur={(e) => {
                    const upstream = e.target.value.trim()
                    if (upstream && upstream !== edited.upstream)
                      patchProfile(edited.id, { upstream })
                  }}
                />
              }
            />
            <Row
              label={strings.settings.profilesUseLabel}
              hint={edited.builtin ? strings.settings.profilesBuiltinNote : undefined}
              control={
                <div className="bb-field-row">
                  {activeIdFor(edited.kind) === edited.id ? (
                    <span className="text-caption">{strings.settings.profilesInUseNote}</span>
                  ) : (
                    <Button variant="ghost" onClick={() => activateProfile(edited)}>
                      {strings.settings.profilesUseButton}
                    </Button>
                  )}
                  {!edited.builtin && (
                    <Button
                      variant="danger"
                      ariaLabel={strings.settings.profilesDeleteAriaLabel}
                      title={strings.settings.profilesDeleteAriaLabel}
                      onClick={() =>
                        confirmThenRun({
                          id: 'delete-profile',
                          title: strings.settings.profilesDeleteConfirmTitle,
                          body: strings.settings.profilesDeleteConfirmBody,
                          confirmLabel: strings.settings.profilesDeleteButton,
                          danger: true,
                          action: () => {
                            setEditingId(null)
                            deleteProfile(edited)
                          },
                        })
                      }
                    >
                      <IconTrash />
                    </Button>
                  )}
                </div>
              }
            />

            {edited.kind === 'ecast' ? ecastRows(edited) : blobcastRows(edited)}

            {/* Everything above about this profile - kind, name, address,
                and its protocol settings - only applies after the bridge
                restarts, so the note belongs once at the end of the editable
                fields, the same place the hostlist modal puts its own. */}
            <p className="text-caption">{strings.settings.profilesRestartNote}</p>
          </>
        )}

        <Row
          label={strings.settings.profilesTransferLabel}
          hint={strings.settings.profilesTransferHint}
          control={
            <Button variant="ghost" onClick={openTransfer}>
              {strings.settings.profilesExportButton} / {strings.settings.profilesImportButton}
            </Button>
          }
        />
        {/* Backend validation lands here now - the address and the rewrite
            fields it checks both live in this section. */}
        {configError && <p className="text-caption bb-diag__error">{configError}</p>}
      </Section>

      <AnimatePresence>
        {transferOpen && (
          <Modal
            title={strings.settings.profilesTransferModalTitle}
            onClose={() => setTransferOpen(false)}
            maxWidth={680}
          >
            <p className="text-body">{strings.settings.profilesTransferModalDescription}</p>
            <textarea
              className="bb-input bb-textarea text-mono"
              rows={12}
              spellCheck={false}
              value={transferText}
              onChange={(e) => setTransferText(e.target.value)}
            />
            {transferReport && (
              <p className="text-caption">
                {t(strings.settings.profilesImportDone, { added: transferReport.added })}
                {transferReport.skipped.length > 0 &&
                  ` · ${t(strings.settings.profilesImportSkipped, {
                    count: transferReport.skipped.length,
                  })}: ${transferReport.skipped.map((s) => `${s.name} (${s.reason})`).join('; ')}`}
              </p>
            )}
            <div className="bb-field-row">
              <Button variant="primary" onClick={() => runTransfer('import_profiles', transferText)}>
                {strings.settings.profilesImportApplyButton}
              </Button>
              <Button variant="ghost" onClick={() => runTransfer('export_profiles_to_file')}>
                {strings.settings.profilesExportFileButton}
              </Button>
              <Button variant="ghost" onClick={() => runTransfer('import_profiles_from_file')}>
                {strings.settings.profilesImportFileButton}
              </Button>
            </div>
            {configError && <p className="text-caption bb-diag__error">{configError}</p>}
          </Modal>
        )}
        {strategyPopupOpen && (
          <Modal title={strings.settings.strategyModalTitle} onClose={closeStrategyPopup} maxWidth={680}>
            {strategyTest === 'running' && (
              <p className="text-body">
                {strategyTotal > 0
                  ? t(strings.settings.strategyModalProgressWithTotal, {
                      done: strategyResults.length,
                      total: strategyTotal,
                    })
                  : strings.settings.strategyModalProgressNoTotal}
                {/* Only worth stating during a "both" run - a single-set run
                    already says which set via the picker above the button. */}
                {targetSet === 'both' && runningStage && (
                  <>
                    {' '}
                    {t(strings.settings.strategyModalStageLabel, {
                      stage: stageLabel(runningStage, strings),
                    })}
                  </>
                )}
              </p>
            )}
            {strategyTest === 'error' && diagError && (
              <p className="text-body bb-diag__error">{diagError}</p>
            )}
            {(['ecast', 'blobcast'] as const)
              .map((stage) => ({ stage, rows: strategyResults.filter((r) => r.targetSet === stage) }))
              .filter(({ rows }) => rows.length > 0)
              .map(({ stage, rows }) => {
                const columns = targetColumns(rows)
                // The stage heading is only worth showing once there's more
                // than one table on screen - a single-set run needs no label
                // repeating what the picker above already said.
                const showHeading = strategyResults.some((r) => r.targetSet !== stage)
                return (
                  <div key={stage} className="bb-strategy-stage">
                    {showHeading && <p className="text-subtitle">{stageLabel(stage, strings)}</p>}
                    <div
                      className="bb-strategy-table"
                      style={{ gridTemplateColumns: `1.4fr repeat(${columns.length}, 1fr) auto` }}
                    >
                      <div className="bb-strategy-table__row bb-strategy-table__row--head">
                        <span>{strings.settings.strategyTableHeaderName}</span>
                        {columns.map((name) => (
                          <span key={name}>{name}</span>
                        ))}
                        <span></span>
                      </div>
                      {rows.map((r) => (
                        <div key={r.key} className="bb-strategy-table__row">
                          <span>
                            {r.name}
                            {r.key === fastestKey && (
                              <span className="bb-strategy-table__badge">
                                {strings.settings.strategyTableFastestBadge}
                              </span>
                            )}
                          </span>
                          {columns.map((name) => (
                            <TargetCell key={name} target={r.targets?.[name]} />
                          ))}
                          <span>
                            <Button variant="ghost" onClick={() => applyStrategy(r.key)}>
                              {strings.settings.strategyTableApplyButton}
                            </Button>
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )
              })}
            {(fastestKey || strategyResults.length > 0) && (
              <div className="bb-strategy-actions">
                {fastestKey && (
                  <Button variant="primary" onClick={() => applyStrategy(fastestKey)}>
                    {strings.settings.strategyModalApplyFastestButton}
                  </Button>
                )}
                {strategyResults.length > 0 && (
                  <>
                    <Button variant="ghost" onClick={() => exportStrategyResults('json')}>
                      {strings.settings.strategyExportJsonButton}
                    </Button>
                    <Button variant="ghost" onClick={() => exportStrategyResults('html')}>
                      {strings.settings.strategyExportHtmlButton}
                    </Button>
                  </>
                )}
              </div>
            )}
            {exportError && <p className="text-caption bb-diag__error">{exportError}</p>}
          </Modal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {hostlistOpen && (
          <Modal
            title={strings.settings.hostlistModalTitle}
            onClose={() => setHostlistOpen(false)}
            maxWidth={560}
          >
            <p className="text-body">{strings.settings.hostlistModalDescription}</p>
            <textarea
              className="bb-textarea text-mono"
              value={hostlist}
              spellCheck={false}
              onChange={(e) => setHostlist(e.target.value)}
            />
            {hostlistError && <p className="text-caption bb-diag__error">{hostlistError}</p>}
            <div className="bb-hostlist__actions">
              <Button variant="primary" onClick={saveHostlist}>
                {strings.settings.hostlistSaveButton}
              </Button>
              <span className="text-caption">{strings.settings.hostlistRestartNote}</span>
            </div>
          </Modal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {updateOpen && (
          <Modal title={strings.settings.updateModalTitle} onClose={closeUpdate} maxWidth={520}>
            {updateProgress?.phase === 'done' && updateProgress.ok ? (
              <>
                <p className="text-body">
                  {t(strings.settings.updateDoneTitle, {
                    version: updateProgress.version ?? '?',
                  })}
                </p>
                <p className="text-caption">
                  {t(strings.settings.updateDoneFiles, {
                    count: updateProgress.applied.length,
                  })}
                </p>
                <ul className="bb-home__diag-steps">
                  {updateProgress.applied.map((name) => (
                    <li key={name} className="text-mono">
                      {name}
                    </li>
                  ))}
                </ul>
                <StrategyChangeReport changes={updateProgress.strategies} />
                <div className="bb-hostlist__actions">
                  <Button
                    variant="primary"
                    onClick={() =>
                      confirmThenRun({
                        id: 'restart-after-update',
                        title: strings.settings.updateRestartTitle,
                        body: strings.settings.updateRestartBody,
                        confirmLabel: strings.settings.updateRestartButton,
                        danger: true,
                        action: () => {
                          callBridge('restart_app').catch(() => {})
                        },
                      })
                    }
                  >
                    {strings.settings.updateRestartButton}
                  </Button>
                </div>
              </>
            ) : updateProgress && !updateProgress.ok ? (
              <p className="text-body bb-diag__error">{updateProgress.error}</p>
            ) : (
              <div className="bb-diag">
                <Spinner size={18} />
                <span className="text-body">
                  {updateProgress?.phase === 'extract'
                    ? strings.settings.updatePhaseExtract
                    : updateProgress?.phase === 'apply'
                      ? strings.settings.updatePhaseApply
                      : strings.settings.updatePhaseDownload}
                  {updateProgress?.phase === 'download' && updateProgress.total > 0 && (
                    <span className="text-mono">
                      {' '}
                      {Math.round((updateProgress.received / updateProgress.total) * 100)}%
                    </span>
                  )}
                </span>
              </div>
            )}
          </Modal>
        )}
      </AnimatePresence>

      <AnimatePresence>
        {confirmRequest && (
          <ConfirmDialog
            title={confirmRequest.title}
            body={confirmRequest.body}
            confirmLabel={confirmRequest.confirmLabel}
            danger={confirmRequest.danger}
            onCancel={() => setConfirmRequest(null)}
            onConfirm={(skipNextTime) => {
              if (skipNextTime) setConfirmSkipped(confirmRequest.id)
              confirmRequest.action()
              setConfirmRequest(null)
            }}
          />
        )}
      </AnimatePresence>

      {/* Sticky rather than fixed: the settings screen is the scroll container's
          content, so this rides the bottom of the viewport without having to
          know the sidebar's width - and it stops at the end of the screen
          instead of floating over the other two. */}
      <AnimatePresence>
        {pendingRestart && (
          <motion.div
            className="bb-settings__dirty"
            role="status"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 12 }}
            transition={transition}
          >
            <span className="bb-settings__dirty-text">
              {pendingRestart === 'app'
                ? strings.settings.dirtyApp
                : strings.settings.dirtyBridge}
            </span>
            <Button variant="secondary" onClick={() => setPendingRestart(null)}>
              {strings.settings.dirtyDismiss}
            </Button>
            <Button onClick={applyPendingRestart} disabled={restarting}>
              {restarting && <Spinner size={14} />}
              {pendingRestart === 'app'
                ? strings.settings.dirtyRestartApp
                : strings.settings.dirtyRestartBridge}
            </Button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

/** What the update did to the strategy files.
 *
 *  Shown rather than merely logged because one of these outcomes is a decision
 *  the user has to know about: `forked` means their edited strategy was kept
 *  untouched and the release's version landed beside it, so nothing they tuned
 *  was lost - but the new one is not in use until they pick it. */
function StrategyChangeReport({ changes }: { changes: StrategyChanges | null }) {
  const strings = useStrings()
  if (!changes) return null
  const { added, updated, forked, skipped } = changes
  if (!added.length && !updated.length && !forked.length && !skipped.length) return null

  return (
    <div className="bb-strategy-changes">
      {added.length > 0 && (
        <p className="text-caption">
          {strings.settings.updateStrategiesAdded}: <span className="text-mono">{added.join(', ')}</span>
        </p>
      )}
      {updated.length > 0 && (
        <p className="text-caption">
          {strings.settings.updateStrategiesUpdated}:{' '}
          <span className="text-mono">{updated.join(', ')}</span>
        </p>
      )}
      {forked.map(([original, fork]) => (
        <p key={fork} className="text-caption bb-strategy-changes__forked">
          {t(strings.settings.updateStrategiesForked, { original, fork })}
        </p>
      ))}
      {skipped.map(([name, reason]) => (
        <p key={name} className="text-caption">
          {t(strings.settings.updateStrategiesSkipped, { name, reason })}
        </p>
      ))}
    </div>
  )
}

/** "Ecast"/"Blobcast" - reusing the same protocol-name strings the profile
 *  kind picker already shows, rather than a second copy of the same two
 *  words under a different key. */
function stageLabel(stage: TargetSet, strings: ReturnType<typeof useStrings>): string {
  return stage === 'ecast' ? strings.settings.profilesKindEcast : strings.settings.profilesKindBlobcast
}

/** Column names for one stage's table, read off the results themselves
 *  rather than hardcoded - a switch-failure row carries an empty targets
 *  object, so this takes the first row that actually has some. Every row in
 *  a stage was probed against the same target list (see _stages_for), so
 *  the first non-empty one is representative of the whole group. */
function targetColumns(rows: StrategyResult[]): string[] {
  for (const row of rows) {
    const names = Object.keys(row.targets ?? {})
    if (names.length > 0) return names
  }
  return []
}

function TargetCell({ target }: { target?: TargetResult }) {
  const strings = useStrings()
  if (!target) return <span className="text-caption">{strings.settings.strategyTableEmptyCell}</span>
  if (!target.ok) {
    return (
      <span className="bb-strategy-table__fail" title={target.error ?? undefined}>
        <IconClose size={14} />
      </span>
    )
  }
  return (
    <span className="bb-strategy-table__ok text-numeric">
      {Math.round(target.elapsedMs ?? 0)} {strings.settings.msUnit}
    </span>
  )
}
