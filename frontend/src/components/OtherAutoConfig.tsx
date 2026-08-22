import { useEffect, useRef, useState } from 'react'
import { Modal } from './Modal'
import { Button } from './Button'
import { Toggle } from './Toggle'
import { Spinner } from './Spinner'
import { callBridge } from '../lib/bridge'
import { useStrings, t } from '../lib/strings'
import { clearPoll } from '../lib/poll'
import './OtherAutoConfig.css'

interface OtherItem {
  kind: 'shortcut' | 'jet'
  path: string
  name: string
  hasBackup: boolean
}

interface ScanProgress {
  started: boolean
  done: boolean
  ok: boolean | null
  error: string | null
  items: OtherItem[]
  foldersChecked: number
}

interface ApplyResult {
  ok: boolean
  error: string | null
  results: Record<string, { ok: boolean; error: string | null }>
}

const SCAN_POLL_MS = 500

type Step =
  | { kind: 'idle' }
  | { kind: 'scanning'; foldersChecked: number }
  | { kind: 'checklist'; items: OtherItem[]; selected: Set<string> }
  | { kind: 'confirming'; items: OtherItem[]; selected: Set<string>; mode: 'apply' | 'revert'; targets: OtherItem[] }
  | { kind: 'applying'; mode: 'apply' | 'revert' }
  | { kind: 'done'; result: ApplyResult; mode: 'apply' | 'revert' }

/**
 * "Настроить автоматически" in the "Прочие копии" tab of ConnectGuide.
 * Mirrors SteamAutoConfig's scan -> checklist -> confirm -> apply/revert
 * shape, but the scan itself is a background job polled for progress (see
 * backend/bridgebox/other_launch.py) since walking every local drive can
 * take minutes, unlike Steam's near-instant VDF read.
 */
export function OtherAutoConfig() {
  const strings = useStrings()
  const [step, setStep] = useState<Step>({ kind: 'idle' })
  const pollRef = useRef<number | undefined>(undefined)

  useEffect(() => () => clearPoll(pollRef), [])

  function failed(mode: 'apply' | 'revert', err: unknown) {
    setStep({
      kind: 'done',
      mode,
      result: { ok: false, error: err instanceof Error ? err.message : String(err), results: {} },
    })
  }

  async function startScan() {
    setStep({ kind: 'scanning', foldersChecked: 0 })
    try {
      await callBridge('start_other_scan')
    } catch (err) {
      failed('apply', err)
      return
    }
    clearPoll(pollRef)
    pollRef.current = window.setInterval(async () => {
      try {
        const progress = await callBridge<ScanProgress>('other_scan_progress')
        if (!progress.done) {
          setStep({ kind: 'scanning', foldersChecked: progress.foldersChecked })
          return
        }
        clearPoll(pollRef)
        if (!progress.ok) {
          failed('apply', progress.error ?? 'scan failed')
          return
        }
        setStep({ kind: 'checklist', items: progress.items, selected: new Set(progress.items.map((i) => i.path)) })
      } catch (err) {
        clearPoll(pollRef)
        failed('apply', err)
      }
    }, SCAN_POLL_MS)
  }

  async function runApply(targets: OtherItem[]) {
    setStep({ kind: 'applying', mode: 'apply' })
    try {
      const result = await callBridge<ApplyResult>(
        'apply_other_launch_options',
        targets.map((i) => ({ kind: i.kind, path: i.path })),
      )
      setStep({ kind: 'done', result, mode: 'apply' })
    } catch (err) {
      failed('apply', err)
    }
  }

  async function runRevert(targets: OtherItem[]) {
    setStep({ kind: 'applying', mode: 'revert' })
    try {
      const result = await callBridge<ApplyResult>(
        'revert_other_launch_options',
        targets.map((i) => ({ kind: i.kind, path: i.path })),
      )
      setStep({ kind: 'done', result, mode: 'revert' })
    } catch (err) {
      failed('revert', err)
    }
  }

  function close() {
    clearPoll(pollRef)
    setStep({ kind: 'idle' })
  }

  function kindLabel(kind: 'shortcut' | 'jet') {
    return kind === 'shortcut' ? strings.home.otherAutoConfigKindShortcut : strings.home.otherAutoConfigKindJet
  }

  return (
    <div className="bb-other-auto">
      <Button variant="primary" className="bb-other-auto__insert-btn" onClick={startScan}>
        {strings.home.otherAutoConfigButton}
      </Button>

      {step.kind === 'scanning' && (
        <Modal title={strings.home.otherAutoConfigButton} onClose={close}>
          <div className="bb-other-auto__progress">
            <Spinner />
            <span className="text-body">{t(strings.home.otherAutoConfigScanning, { count: step.foldersChecked })}</span>
          </div>
        </Modal>
      )}

      {step.kind === 'checklist' && (
        <Modal title={strings.home.otherAutoConfigButton} onClose={close}>
          {step.items.length === 0 ? (
            <p className="text-body">{strings.home.otherAutoConfigNoneFound}</p>
          ) : (
            <>
              <ul className="bb-other-auto__list">
                {step.items.map((item) => (
                  <li key={item.path} className="bb-other-auto__row">
                    <div className="bb-other-auto__label">
                      <Toggle
                        checked={step.selected.has(item.path)}
                        onChange={(checked) => {
                          const selected = new Set(step.selected)
                          if (checked) selected.add(item.path)
                          else selected.delete(item.path)
                          setStep({ ...step, selected })
                        }}
                        label={item.name}
                      />
                      <span className="text-body">{item.name}</span>
                      <span className="bb-other-auto__kind">{kindLabel(item.kind)}</span>
                    </div>
                    {item.hasBackup && (
                      <Button
                        variant="ghost"
                        onClick={() =>
                          setStep({ kind: 'confirming', items: step.items, selected: step.selected, mode: 'revert', targets: [item] })
                        }
                      >
                        {strings.home.otherAutoConfigRevertButton}
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
              <Button
                variant="primary"
                disabled={step.selected.size === 0}
                onClick={() =>
                  setStep({
                    kind: 'confirming',
                    items: step.items,
                    selected: step.selected,
                    mode: 'apply',
                    targets: step.items.filter((i) => step.selected.has(i.path)),
                  })
                }
              >
                {strings.home.otherAutoConfigConfirmButton}
              </Button>
            </>
          )}
        </Modal>
      )}

      {step.kind === 'confirming' && (
        <Modal title={strings.home.otherAutoConfigConfirmTitle} onClose={close}>
          <p className="text-body">{t(strings.home.otherAutoConfigConfirmBody, { count: step.targets.length })}</p>
          <div className="bb-other-auto__actions">
            <Button variant="ghost" onClick={() => setStep({ kind: 'checklist', items: step.items, selected: step.selected })}>
              {strings.common.cancel}
            </Button>
            <Button variant="primary" onClick={() => (step.mode === 'apply' ? runApply(step.targets) : runRevert(step.targets))}>
              {strings.home.otherAutoConfigConfirmButton}
            </Button>
          </div>
        </Modal>
      )}

      {step.kind === 'applying' && (
        <Modal title={strings.home.otherAutoConfigButton} onClose={close}>
          <Spinner label={step.mode === 'apply' ? strings.home.otherAutoConfigApplying : strings.home.otherAutoConfigReverting} />
        </Modal>
      )}

      {step.kind === 'done' && (
        <Modal title={strings.home.otherAutoConfigDone} onClose={close}>
          <ul className="bb-other-auto__list">
            {Object.entries(step.result.results).map(([path, r]) => (
              <li key={path} className="bb-other-auto__row">
                {path}: {r.ok ? '✓' : r.error}
              </li>
            ))}
          </ul>
          {step.result.error && <p className="text-body">{step.result.error}</p>}
          <Button variant="primary" onClick={close}>
            {strings.common.close}
          </Button>
        </Modal>
      )}
    </div>
  )
}
