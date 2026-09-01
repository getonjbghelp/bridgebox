// Every shape SettingsScreen.tsx and its extracted panels (StrategyTestPanel,
// ...) share - split out purely to stop the screen's own file from being
// 250+ lines of type declarations before a single hook runs. No behavior
// here, just names.
import type { UiSection } from '../../state/MotionPrefsContext'

export interface StrategyOption {
  key: string
  name: string
  aggressive: boolean
}
export type StrategyGroups = Record<string, StrategyOption[]>

export interface RewriteConfig {
  server_enabled: boolean
  server_keys: string[]
  room_id_keys: string[]
  upstream_base: string
  origin_enabled: boolean
  upstream_origin: string
  user_agent_enabled: boolean
  fallback_user_agent: string
}

export interface TempDirResponse {
  ok: boolean
  error: string | null
  path: string
  resolved: string
}

export interface UpdateCheckResponse {
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
export interface StrategyChanges {
  added: string[]
  updated: string[]
  forked: [string, string][]
  skipped: [string, string][]
}

export interface UpdateProgressResponse {
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
export type ProfileKind = 'ecast' | 'blobcast'

/** Response rewriting. Only ever applied to Ecast, which is why it lives on
 *  the profile instead of being a global section that silently applied to
 *  Blobcast traffic too. */
export interface EcastSettings {
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

export interface BlobcastSettings {
  socketio_port: number
  intercept_session: boolean
  local_server_name: string
  log_frames: boolean
  paths: string[]
}

export interface Profile {
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
export type EcastField = 'server_keys' | 'room_id_keys' | 'upstream_origin' | 'fallback_user_agent'
export type EcastFlag = 'server_enabled' | 'origin_enabled' | 'user_agent_enabled'

export interface ProfilesConfig {
  items: Profile[]
  active_ecast: string
  active_blobcast: string
}

export interface AutostartResponse {
  ok: boolean
  error: string | null
  enabled: boolean
  minimized: boolean
}

export interface AppConfig {
  server: { port: number }
  zapret: { strategy: string; hide_console: boolean }
  update: { check_on_startup: boolean }
  app_update: { check_on_startup: boolean }
  health_check: { enabled: boolean }
  ui?: { start_bridge_on_launch: boolean; minimize_to_tray: boolean }
  profiles: ProfilesConfig
  rewrite: RewriteConfig
}

export interface AppUpdateCheckResponse {
  ok: boolean
  error: string | null
  installed: string | null
  latest: string | null
  notes: string | null
  htmlUrl: string | null
  critical: boolean
  updateAvailable: boolean
}

export interface AppApplyProgress {
  started: boolean
  done: boolean
  ok: boolean | null
  error: string | null
  version: string | null
}

export interface ConfigResponse {
  ok: boolean
  error: string | null
  config: AppConfig | null
}

// Only the factory-reset response needs `ui` - every other call site here
// edits a single non-ui field and has no reason to touch theme/animations/
// sidebar state.
export interface FullConfigResponse {
  ok: boolean
  error: string | null
  config: (AppConfig & { ui: UiSection }) | null
}

export interface StrategiesResponse {
  ok: boolean
  groups: StrategyGroups
}

export interface TargetResult {
  ok: boolean
  elapsedMs: number | null
  status: number | null
  error: string | null
}

/** Which protocol's hosts a result was measured against - "both" runs two
 *  full passes (see backend Api.test_strategies), so a flat results list can
 *  hold rows from either stage and this is what tells them apart. */
export type TargetSet = 'ecast' | 'blobcast'

export interface StrategyResult {
  key: string
  name: string
  ok: boolean
  error: string | null
  targets: Record<string, TargetResult>
  targetSet: TargetSet
}

export interface TestStrategiesStart {
  ok: boolean
  error: string | null
  total: number
}

export interface TestStrategiesProgress {
  ok: boolean
  error: string | null
  results: StrategyResult[]
  fastestKey: string | null
  // Which stage is running right now; null before a run starts and once it
  // finishes. Lets the popup say "Этап: Blobcast" during a "both" run.
  stage: TargetSet | null
  done: boolean
}

export interface StartupUpdateCheck {
  ok: boolean
  error: string | null
  started: boolean
  done: boolean
  installed: string | null
  latest: string | null
  updateAvailable: boolean
}

export interface HostlistResponse {
  ok: boolean
  error: string | null
  text: string
}

export interface SaveHostlistResponse {
  ok: boolean
  error: string | null
  count: number
}

export interface ConfirmRequest {
  // localStorage key for "don't ask again" - each reset button owns its own,
  // so skipping one confirmation never silences another.
  id: string
  title: string
  body: string
  confirmLabel: string
  danger?: boolean
  action: () => void
}

export type SettingsTab = 'general' | 'network' | 'profiles' | 'updates' | 'system'
