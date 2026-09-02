import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Section, Row } from '../components/Section'
import { Toggle } from '../components/Toggle'
import { Button } from '../components/Button'
import { Modal } from '../components/Modal'
import { ConfirmDialog } from '../components/ConfirmDialog'
import { DiagBadge, type DiagState } from '../components/DiagBadge'
import { IconFlagGb, IconFlagRu } from '../components/icons'
import { Spinner } from '../components/Spinner'
import { ProgressBar } from '../components/ProgressBar'
import { Segmented } from '../components/Segmented'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { useSpringTransition, useStepTransition } from '../lib/motion'
import { useStrings, t, strategyGroupLabel } from '../lib/strings'
import { callBridge, isNativeBridgeAvailable, logBridgeError, waitForBridgeReady } from '../lib/bridge'
import { isConfirmSkipped, setConfirmSkipped } from '../lib/confirmSkip'
import { clearPoll } from '../lib/poll'
import { useStrategyTestPanel } from './settings/StrategyTestPanel'
import { useProfilesTab } from './settings/ProfilesTab'
import type {
  StrategyGroups,
  TempDirResponse,
  UpdateCheckResponse,
  StrategyChanges,
  UpdateProgressResponse,
  ProfilesConfig,
  AutostartResponse,
  AppConfig,
  AppUpdateCheckResponse,
  AppApplyProgress,
  ConfigResponse,
  FullConfigResponse,
  StrategiesResponse,
  StartupUpdateCheck,
  HostlistResponse,
  SaveHostlistResponse,
  ConfirmRequest,
  SettingsTab,
} from './settings/types'
import './SettingsScreen.css'

// Shared with StrategyTestPanel's own copy - two different pollers (this one
// is the zapret-update download progress) that happen to want the same
// cadence, not one poller split across two files.
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
  health_check: 'bridge',
}
// Left-to-right order the tab bar itself shows them in - the only source of
// truth for which way a switch should slide, so the animation never drifts
// out of sync with a future reordering of the Segmented's own options.
const TAB_ORDER: SettingsTab[] = ['general', 'system', 'network', 'updates', 'profiles']

// Which visible section a RESTART_SCOPE key's own controls live in, so the
// dirty banner can name what changed instead of a bare "settings changed".
// A component-body const, not module-level like RESTART_SCOPE itself,
// because the labels are translated strings.
function restartScopeLabels(strings: ReturnType<typeof useStrings>): Record<string, string> {
  return {
    server: strings.settings.systemSectionTitle,
    zapret: strings.settings.networkSectionTitle,
    profiles: strings.settings.profilesSectionTitle,
    proxy: strings.settings.networkSectionTitle,
    rewrite: strings.settings.profilesSectionTitle,
    logging: strings.settings.systemSectionTitle,
    health_check: strings.settings.networkSectionTitle,
  }
}

export function SettingsScreen() {
  const strings = useStrings()
  const {
    animationsEnabled,
    setAnimationsEnabled,
    animationDurationMs,
    setAnimationDurationMs,
    theme,
    setTheme,
    language,
    setLanguage,
    setSidebarCollapsed,
    setSetupComplete,
  } = useMotionPrefs()

  // Free-text buffer for the ms field below - a Toggle/Segmented commits on
  // every click, but a number needs to be typeable ("2" then "20") without
  // firing setAnimationDurationMs on every keystroke. Resynced whenever the
  // context value changes from elsewhere (config load, factory reset).
  const [animationDurationInput, setAnimationDurationInput] = useState(String(animationDurationMs))
  useEffect(() => {
    setAnimationDurationInput(String(animationDurationMs))
  }, [animationDurationMs])

  function commitAnimationDuration() {
    const parsed = Number(animationDurationInput)
    const clamped = Number.isFinite(parsed) ? Math.min(1000, Math.max(50, parsed)) : animationDurationMs
    setAnimationDurationInput(String(clamped))
    if (clamped !== animationDurationMs) setAnimationDurationMs(clamped)
  }

  const [strategy, setStrategy] = useState('general')
  const [strategyGroups, setStrategyGroups] = useState<StrategyGroups>({})
  const selectedStrategyIsAggressive = Object.values(strategyGroups)
    .flat()
    .some((opt) => opt.key === strategy && opt.aggressive)
  const [port, setPort] = useState('8443')
  // The last value actually confirmed by the backend (set on load and on
  // every successful persist) - not just what the input currently shows, so
  // focusing the field and blurring without typing anything can be told
  // apart from a real edit. See handlePortBlur.
  const committedPortRef = useRef('8443')
  const [hideConsole, setHideConsole] = useState(true)
  const [healthCheckEnabled, setHealthCheckEnabled] = useState(true)
  // Autostart is the one setting whose truth lives outside config.yaml - it is
  // a Windows scheduled task, and the task is what counts.
  const [autostart, setAutostart] = useState({ enabled: false, minimized: false })
  const [autostartError, setAutostartError] = useState<string | null>(null)
  const [startBridgeOnLaunch, setStartBridgeOnLaunch] = useState(false)
  const [minimizeToTray, setMinimizeToTray] = useState(true)

  const [profiles, setProfiles] = useState<ProfilesConfig | null>(null)
  const [configError, setConfigError] = useState<string | null>(null)

  const [tempDir, setTempDir] = useState<TempDirResponse | null>(null)
  const [checkOnStartup, setCheckOnStartup] = useState(false)
  const [updateCheck, setUpdateCheck] = useState<DiagState>('idle')
  const [updateInfo, setUpdateInfo] = useState<UpdateCheckResponse | null>(null)
  const [updateOpen, setUpdateOpen] = useState(false)
  const [updateProgress, setUpdateProgress] = useState<UpdateProgressResponse | null>(null)
  const updatePollRef = useRef<number | undefined>(undefined)

  // BridgeBox's own release check - separate from the zapret one above.
  // AppUpdateBanner already polls this in the background app-wide; this is
  // just the manual "Проверить сейчас" + toggle, same shape as zapret's.
  const [appCheckOnStartup, setAppCheckOnStartup] = useState(false)
  const [appUpdateCheck, setAppUpdateCheck] = useState<DiagState>('idle')
  const [appUpdateInfo, setAppUpdateInfo] = useState<AppUpdateCheckResponse | null>(null)
  // Self-update (download + swap the running .exe) - separate from the check
  // above, which only asks "is there something newer".
  const [appApplyState, setAppApplyState] = useState<DiagState>('idle')
  const [appApplyError, setAppApplyError] = useState<string | null>(null)
  const [appApplyProgress, setAppApplyProgress] = useState<{
    phase: AppApplyProgress['phase']
    received: number
    total: number
  }>({ phase: 'idle', received: 0, total: 0 })
  const appApplyPollRef = useRef<number | undefined>(undefined)

  const [hostlistOpen, setHostlistOpen] = useState(false)
  const [hostlist, setHostlist] = useState('')
  const [hostlistError, setHostlistError] = useState<string | null>(null)

  const [confirmRequest, setConfirmRequest] = useState<ConfirmRequest | null>(null)
  // Which restart the settings changed so far are waiting on, if any. See
  // RESTART_SCOPE for why this is not simply "something changed".
  const [pendingRestart, setPendingRestart] = useState<'bridge' | 'app' | null>(null)
  // Which RESTART_SCOPE section(s) contributed, so the banner can name them
  // instead of a bare "settings changed" - a user who tweaked strategy AND
  // port before noticing the banner had no way to tell which one it meant.
  // Accumulates across edits (the banner itself persists across them, by
  // design) and clears only when the pending restart is resolved or dismissed.
  const [changedSections, setChangedSections] = useState<Set<string>>(new Set())
  const [restarting, setRestarting] = useState(false)
  const transition = useSpringTransition()
  // Same swap speed as the setup wizard's own step transitions, not a
  // separate faster tuning - see useStepTransition's own comment.
  const tabTransition = useStepTransition()
  const [activeTab, setActiveTab] = useState<SettingsTab>('general')
  // Which way the incoming tab should slide in from: 1 (right) when moving
  // to a tab further along the bar, -1 (left) moving back toward "Общие" -
  // so the motion mirrors the click's position on the tab bar itself instead
  // of being the same for every switch.
  const [tabDirection, setTabDirection] = useState(1)

  function changeTab(next: SettingsTab) {
    setTabDirection(TAB_ORDER.indexOf(next) < TAB_ORDER.indexOf(activeTab) ? -1 : 1)
    setActiveTab(next)
  }

  // Leaving the screen mid-run must not keep polling a dead component.
  useEffect(() => () => stopUpdatePolling(), [])
  useEffect(() => () => stopAppApplyPolling(), [])

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

  // Same shape as the zapret poll above, but for BridgeBox's own release
  // check - main() already fires this in the background (see
  // Api.start_app_update_check), so this just picks up the result once it
  // lands, which is also how the version row gets a value without the user
  // clicking "Проверить сейчас" first.
  useEffect(() => {
    let cancelled = false
    let attempts = 0

    async function poll() {
      if (cancelled || !isNativeBridgeAvailable()) return
      const result = await callBridge<AppUpdateCheckResponse & { started: boolean; done: boolean }>(
        'app_update_check',
      )
      if (cancelled || !result.started) return
      if (!result.done) {
        attempts += 1
        if (attempts < 20) window.setTimeout(poll, 1000)
        return
      }
      setAppUpdateInfo(result)
      setAppUpdateCheck(result.ok ? 'done' : 'error')
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
    setHealthCheckEnabled(config.health_check.enabled)
    setCheckOnStartup(config.update.check_on_startup)
    setAppCheckOnStartup(config.app_update.check_on_startup)
    setProfiles(config.profiles)
    if (config.ui) {
      setStartBridgeOnLaunch(config.ui.start_bridge_on_launch)
      setMinimizeToTray(config.ui.minimize_to_tray)
    }
  }

  async function noteChange(patch: Record<string, unknown>) {
    const scopedKeys = Object.keys(patch).filter((key) => RESTART_SCOPE[key])
    if (scopedKeys.length === 0) return
    const scopes = scopedKeys.map((key) => RESTART_SCOPE[key])
    setChangedSections((prev) => new Set([...prev, ...scopedKeys]))

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
      logBridgeError(err)
      setAutostart({ enabled: false, minimized })
      setAutostartError(strings.common.unexpectedError)
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
      logBridgeError(err)
      setConfigError(strings.common.unexpectedError)
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
  function dismissPendingRestart() {
    setPendingRestart(null)
    setChangedSections(new Set())
  }

  async function applyPendingRestart() {
    const scope = pendingRestart
    if (scope === null || !isNativeBridgeAvailable()) {
      dismissPendingRestart()
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
      dismissPendingRestart()
    } catch (err) {
      logBridgeError(err)
      setConfigError(strings.common.unexpectedError)
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
      app_update: null,
      health_check: null,
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
    if (!request.hideSkip && isConfirmSkipped(request.id)) {
      request.action()
      return
    }
    setConfirmRequest(request)
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

  async function runAppUpdateCheck() {
    setAppUpdateCheck('running')
    setAppUpdateInfo(null)
    if (!isNativeBridgeAvailable()) return setAppUpdateCheck('idle')
    const result = await callBridge<AppUpdateCheckResponse>('check_app_update')
    setAppUpdateInfo(result)
    setAppUpdateCheck(result.ok ? 'done' : 'error')
  }

  function openAppReleasePage() {
    if (!appUpdateInfo?.htmlUrl || !isNativeBridgeAvailable()) return
    callBridge('open_external_url', appUpdateInfo.htmlUrl).catch(() => {})
  }

  function stopAppApplyPolling() {
    clearPoll(appApplyPollRef)
  }

  async function runAppApplyUpdate() {
    if (!isNativeBridgeAvailable()) return
    setAppApplyState('running')
    setAppApplyError(null)
    setAppApplyProgress({ phase: 'download', received: 0, total: 0 })
    await callBridge('start_app_apply_update').catch(() => {})
    stopAppApplyPolling()
    appApplyPollRef.current = window.setInterval(async () => {
      try {
        const progress = await callBridge<AppApplyProgress>('app_apply_progress')
        setAppApplyProgress({
          phase: progress.phase,
          received: progress.received,
          total: progress.total,
        })
        if (!progress.done) return
        stopAppApplyPolling()
        if (progress.ok) {
          setAppApplyState('done')
        } else {
          setAppApplyState('error')
          setAppApplyError(progress.error)
        }
      } catch (err) {
        logBridgeError(err)
        stopAppApplyPolling()
        setAppApplyState('error')
        setAppApplyError(strings.common.unexpectedError)
      }
    }, 1000)
  }

  function restartAfterAppUpdate() {
    confirmThenRun({
      id: 'restart-after-app-update',
      title: strings.settings.updateRestartTitle,
      body: strings.settings.updateRestartBody,
      confirmLabel: strings.settings.updateRestartButton,
      danger: true,
      action: () => {
        // Not restart_app: applying a self-update needs the dedicated
        // relaunch helper (see Api.restart_after_app_update) - the swap
        // cannot happen in this process, see app_update.py's own docstring.
        callBridge('restart_after_app_update').catch(() => {})
      },
    })
  }

  function stopUpdatePolling() {
    clearPoll(updatePollRef)
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
        logBridgeError(err)
        stopUpdatePolling()
        setUpdateProgress({
          ok: false, error: strings.common.unexpectedError, done: true, phase: 'idle',
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

  // Shared by the tab panel's motion.div below. The entrance slides in from
  // the side the clicked tab sits on relative to the one leaving - right when
  // moving further along the bar, left moving back - so the motion mirrors
  // where the click landed. Only the entrance is directional, same reasoning
  // as the setup wizard's own step transitions (see useStepTransition):
  // AnimatePresence keeps the outgoing element's exit props from ITS OWN last
  // render, which can be a stale direction left over from an earlier switch,
  // not the one happening now - directional exits would occasionally slide
  // the wrong way. Which side it entered from was never load-bearing either.
  const tabPanelMotion = {
    initial: { opacity: 0, x: tabDirection * 18 },
    animate: { opacity: 1, x: 0, transition: tabTransition.in },
    exit: { opacity: 0, transition: tabTransition.out },
  }

  // Both hooks split their return into a tab-gated part and a modal that
  // must survive switching tabs - see each hook's own docstring for why.
  const strategyTest = useStrategyTestPanel(handleStrategyChange)
  const profilesTab = useProfilesTab({
    profiles,
    persistChecked,
    applyConfig,
    confirmThenRun,
    configError,
    setConfigError,
  })

  return (
    <div>
      <h1 className="text-display" style={{ marginBottom: 'var(--space-4)' }}>
        {strings.settings.title}
      </h1>

      <div className="bb-settings-tabs">
        <Segmented<SettingsTab>
          value={activeTab}
          onChange={changeTab}
          ariaLabel={strings.settings.tabsAriaLabel}
          options={[
            { value: 'general', label: strings.settings.tabGeneral },
            { value: 'system', label: strings.settings.systemSectionTitle },
            { value: 'network', label: strings.settings.tabNetwork },
            { value: 'updates', label: strings.settings.tabUpdates },
            { value: 'profiles', label: strings.settings.tabProfiles },
          ]}
        />
      </div>

      {/* One AnimatePresence around the whole switch, keyed by activeTab, not
          one per tab: six independent AnimatePresences (the first version of
          this) each fire their own exit/enter at the same instant, so the
          outgoing tab's Sections and the incoming tab's Sections are BOTH in
          the DOM for the ~0.13s exit - taking up layout space simultaneously,
          which briefly grows the page past its normal height and pops the
          scrollbar in and out. mode="wait" on ONE shared instance forces the
          old content to fully leave before the new content mounts, so only
          one tab's height is ever on screen at a time. */}
      <AnimatePresence mode="wait" initial={false}>
      <motion.div key={activeTab} {...tabPanelMotion}>
      {activeTab === 'general' && (
      <>
      <Section
        title={strings.settings.appearanceSectionTitle}
        description={strings.settings.appearanceSectionDescription}
      >
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
                { value: 'ru', label: <IconFlagRu />, ariaLabel: 'Русский' },
                { value: 'en', label: <IconFlagGb />, ariaLabel: 'English' },
              ]}
            />
          }
        />
        <Row
          label={strings.settings.darkThemeLabel}
          control={
            <Toggle
              checked={theme === 'dark'}
              onChange={(v) => setTheme(v ? 'dark' : 'light')}
              label={strings.settings.darkThemeLabel}
            />
          }
        />
        <Row
          label={strings.settings.animationsLabel}
          control={
            <Toggle
              checked={animationsEnabled}
              onChange={setAnimationsEnabled}
              label={strings.settings.animationsLabel}
            />
          }
        />
        <Row
          label={strings.settings.animationDurationLabel}
          hint={strings.settings.animationDurationHint}
          control={
            <input
              className="bb-input bb-input--narrow text-mono"
              value={animationDurationInput}
              disabled={!animationsEnabled}
              onChange={(e) => setAnimationDurationInput(e.target.value.replace(/\D/g, ''))}
              onBlur={commitAnimationDuration}
              inputMode="numeric"
              aria-label={strings.settings.animationDurationLabel}
            />
          }
        />
      </Section>

      <Section
        title={strings.settings.startupSectionTitle}
        description={strings.settings.startupSectionDescription}
      >
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
              label={strings.settings.autostartLabel}
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
                label={strings.settings.autostartMinimizedLabel}
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
              label={strings.settings.startBridgeOnLaunchLabel}
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
              label={strings.settings.minimizeToTrayLabel}
            />
          }
        />
        {autostartError && <p className="text-caption bb-diag__error">{autostartError}</p>}
      </Section>
      </>
      )}

      {activeTab === 'network' && (
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
                        {opt.aggressive ? strings.settings.strategyAggressiveMarker : ''}
                        {opt.name}
                      </option>
                    ))}
                  </optgroup>
                ))}
            </select>
          }
        />
        {/* A native <select>'s options can't carry a tooltip, only the ⚠
            prefix above - so the actual explanation for the CURRENTLY chosen
            strategy sits here, where it's impossible to miss right after
            picking one. Confirmed against a real regression: Alternative 5's
            unfooled syndata desync corrupted Blobcast's WebSocket ~20-30s in
            (see zapret/strategies.py's _is_aggressive doc comment). */}
        {selectedStrategyIsAggressive && (
          <div className="bb-strategy-warning">{strings.settings.strategyAggressiveWarning}</div>
        )}
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
        {strategyTest.trigger}
        <Row
          label={strings.settings.connHealthLabel}
          hint={strings.settings.connHealthHint}
          control={
            <Toggle
              checked={healthCheckEnabled}
              onChange={(v) => {
                setHealthCheckEnabled(v)
                persist({ health_check: { enabled: v } })
              }}
              label={strings.settings.connHealthLabel}
            />
          }
        />
      </Section>
      )}

      {activeTab === 'updates' && (
      <>
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
              label={strings.settings.updateCheckOnStartupLabel}
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
        title={strings.settings.appUpdateSectionTitle}
        description={strings.settings.appUpdateSectionDescription}
      >
        <Row
          label={strings.settings.appUpdateVersionLabel}
          control={<span className="text-mono">{appUpdateInfo?.installed ?? '—'}</span>}
        />
        <Row
          label={strings.settings.updateCheckOnStartupLabel}
          hint={strings.settings.appUpdateCheckOnStartupHint}
          control={
            <Toggle
              checked={appCheckOnStartup}
              onChange={(v) => {
                setAppCheckOnStartup(v)
                persist({ app_update: { check_on_startup: v } })
              }}
              label={strings.settings.updateCheckOnStartupLabel}
            />
          }
        />
        <Row
          label={strings.settings.updateCheckLabel}
          hint={strings.settings.appUpdateCheckHint}
          control={
            <div className="bb-diag">
              <Button
                variant="secondary"
                onClick={runAppUpdateCheck}
                disabled={appUpdateCheck === 'running'}
              >
                {appUpdateCheck === 'running'
                  ? strings.settings.updateCheckButtonRunning
                  : strings.settings.updateCheckButtonIdle}
              </Button>
              <DiagBadge state={appUpdateCheck} />
            </div>
          }
        />
        {appUpdateInfo?.ok && appUpdateInfo.updateAvailable && appApplyState !== 'done' && (
          <Row
            label={strings.settings.appUpdateApplyLabel}
            hint={
              appApplyState === 'error'
                ? t(strings.appUpdate.applyFailed, { error: appApplyError ?? '' })
                : strings.settings.appUpdateApplyHint
            }
            control={
              appApplyState === 'running' ? (
                <div className="bb-update-progress">
                  <span className="text-caption">
                    {appApplyProgress.phase === 'verify'
                      ? strings.appUpdate.phaseVerify
                      : appApplyProgress.phase === 'extract'
                        ? strings.appUpdate.phaseExtract
                        : strings.appUpdate.phaseDownload}
                    {appApplyProgress.phase === 'download' && appApplyProgress.total > 0 && (
                      <span className="text-mono">
                        {' '}
                        {Math.round((appApplyProgress.received / appApplyProgress.total) * 100)}%
                      </span>
                    )}
                  </span>
                  <ProgressBar
                    percent={
                      appApplyProgress.phase === 'download' && appApplyProgress.total > 0
                        ? (appApplyProgress.received / appApplyProgress.total) * 100
                        : null
                    }
                  />
                </div>
              ) : (
                <div className="bb-diag">
                  <Button variant="primary" onClick={runAppApplyUpdate}>
                    {strings.settings.appUpdateApplyButton}
                  </Button>
                  {appApplyState === 'error' && (
                    <Button variant="ghost" onClick={openAppReleasePage}>
                      {strings.settings.appUpdateOpenReleaseButton}
                    </Button>
                  )}
                </div>
              )
            }
          />
        )}
        {appApplyState === 'done' && (
          <Row
            label={strings.settings.appUpdateApplyLabel}
            hint={strings.settings.appUpdateApplyDoneHint}
            control={
              <Button variant="danger" onClick={restartAfterAppUpdate}>
                {strings.settings.updateRestartButton}
              </Button>
            }
          />
        )}
        {appUpdateInfo?.ok && appUpdateInfo.updateAvailable && appUpdateInfo.critical && (
          <p className="text-caption bb-diag__error">
            {strings.settings.appUpdateCriticalNote}
          </p>
        )}
        {appUpdateInfo?.ok && !appUpdateInfo.updateAvailable && appUpdateInfo.latest && (
          <p className="text-caption">
            {t(strings.settings.updateUpToDate, { version: appUpdateInfo.latest })}
          </p>
        )}
        {appUpdateInfo?.ok && appUpdateInfo.updateAvailable && (
          <p className="text-caption">
            {t(strings.settings.updateAvailable, {
              latest: appUpdateInfo.latest ?? '?',
              installed: appUpdateInfo.installed ?? '?',
            })}
          </p>
        )}
        {appUpdateInfo && !appUpdateInfo.ok && (
          <p className="text-caption bb-diag__error">{appUpdateInfo.error}</p>
        )}
      </Section>
      </>
      )}

      {activeTab === 'profiles' && profilesTab.section}

      {activeTab === 'system' && (
      <>
      <Section
        title={strings.settings.systemSectionTitle}
        description={strings.settings.systemSectionDescription}
      >
        <Row
          label={strings.settings.hideConsoleLabel}
          hint={strings.settings.hideConsoleHint}
          control={
            <Toggle
              checked={hideConsole}
              onChange={handleHideConsoleChange}
              label={strings.settings.hideConsoleLabel}
            />
          }
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
      </Section>

      {/* Folded into the "Система" tab rather than kept as its own: with
          sections grouped into tabs there is no longer a long scroll to fall
          to the end of, and putting it beside System keeps the two
          rarely-touched, consequential settings together instead of adding a
          sixth tab for one button. Still its own <Section>, last within the
          tab, so the red-bordered chrome reads as a deliberate final step,
          not something to breeze past on the way to the port field. */}
      <Section
        title={strings.settings.dangerZoneSectionTitle}
        description={strings.settings.dangerZoneSectionDescription}
      >
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
                  hideSkip: true,
                  action: factoryReset,
                })
              }
            >
              {strings.settings.factoryResetButton}
            </Button>
          }
        />
      </Section>
      </>
      )}
      </motion.div>
      </AnimatePresence>

      {profilesTab.modal}
      {strategyTest.modal}

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
              <div className="bb-update-progress">
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
                <ProgressBar
                  percent={
                    updateProgress?.phase === 'download' && updateProgress.total > 0
                      ? (updateProgress.received / updateProgress.total) * 100
                      : null
                  }
                />
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
            hideSkip={confirmRequest.hideSkip}
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
              {t(pendingRestart === 'app' ? strings.settings.dirtyApp : strings.settings.dirtyBridge, {
                sections:
                  // Unique, in first-changed order (Set preserves insertion
                  // order) - "Сеть и обход, Профили подключения", not a
                  // bare "Настройки изменены" that could mean anything from
                  // three edits ago.
                  [...new Set(
                    [...changedSections].map((key) => restartScopeLabels(strings)[key]),
                  )].join(', '),
              })}
            </span>
            <Button variant="secondary" onClick={dismissPendingRestart}>
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
