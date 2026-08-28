import { useState } from 'react'
import { AnimatePresence } from 'framer-motion'
import { Section, Row } from '../../components/Section'
import { Toggle } from '../../components/Toggle'
import { Button } from '../../components/Button'
import { Modal } from '../../components/Modal'
import { Segmented } from '../../components/Segmented'
import { IconPlus, IconTrash } from '../../components/icons'
import { useStrings, t } from '../../lib/strings'
import { callBridge, isNativeBridgeAvailable } from '../../lib/bridge'
import type {
  AppConfig,
  BlobcastSettings,
  ConfirmRequest,
  EcastField,
  EcastFlag,
  EcastSettings,
  Profile,
  ProfileKind,
  ProfilesConfig,
} from './types'

const splitKeys = (text: string) =>
  text
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)

interface ProfilesTabArgs {
  profiles: ProfilesConfig | null
  /** Owned by the parent because a rejected patch echoes the PREVIOUS config
   *  back (see SettingsScreen's own docstring on persistChecked) - the parent
   *  is what applies that echo across every tab's state, this one included. */
  persistChecked: (patch: Record<string, unknown>) => Promise<void>
  /** Only import/export go through this directly rather than persistChecked -
   *  their response carries `report`/`json` instead of the validation shape
   *  persistChecked expects. */
  applyConfig: (config: AppConfig) => void
  confirmThenRun: (request: ConfirmRequest) => void
  configError: string | null
  setConfigError: (error: string | null) => void
}

/** A hook rather than a component, same reasoning as useStrategyTestPanel:
 *  the transfer modal must not unmount (and lose its draft text) when the
 *  user switches away from the "Профили" tab, so its state has to outlive
 *  the tab-gated section that opens it. `section` renders inside the
 *  activeTab === 'profiles' block; `modal` renders once, unconditionally,
 *  alongside the screen's other modals. */
export function useProfilesTab({
  profiles,
  persistChecked,
  applyConfig,
  confirmThenRun,
  configError,
  setConfigError,
}: ProfilesTabArgs) {
  const strings = useStrings()
  const [editingId, setEditingId] = useState<string | null>(null)
  const [transferOpen, setTransferOpen] = useState(false)
  const [transferText, setTransferText] = useState('')
  const [transferReport, setTransferReport] = useState<{
    added: number
    skipped: { name: string; reason: string }[]
  } | null>(null)

  const activeIdFor = (kind: ProfileKind) =>
    kind === 'ecast' ? profiles?.active_ecast : profiles?.active_blobcast

  const profilesOfKind = (kind: ProfileKind) =>
    profiles?.items.filter((p) => p.kind === kind) ?? []

  // Which profile the panel is EDITING - deliberately not the same question
  // as which one is in use. There can be several profiles of a kind and only
  // one is active, so editing an inactive one has to be possible; conflating
  // the two would mean a profile could only be configured by first switching
  // the running bridge onto it.
  const edited = profiles?.items.find((p) => p.id === editingId) ?? profiles?.items[0] ?? null

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

        {/* Collapsed by default: the block's own hint already admits direct
            connections work fine without it, so it's the densest, most
            jargon-heavy stretch of the screen (JSON field names, balancer
            headers) sitting unconditionally open for people who will never
            touch it. <details> over a useState toggle - free keyboard/screen-
            reader semantics, no state to wire up. */}
        <details className="bb-subheading--collapsible">
          <summary className="bb-subheading__summary">
            <span className="text-subtitle">{strings.settings.profilesEcastSettingsTitle}</span>
            <span className="text-caption">{strings.settings.profilesEcastSettingsHint}</span>
          </summary>
          <div className="bb-subheading__content">
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
          </div>
        </details>
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

  const section = (
    <>
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
    </>
  )

  const modal = (
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
    </AnimatePresence>
  )

  return { section, modal }
}
