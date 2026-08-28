import { useState } from 'react'
import { Modal } from './Modal'
import { Button } from './Button'
import { Toggle } from './Toggle'
import { Spinner } from './Spinner'
import { callBridge, logBridgeError } from '../lib/bridge'
import { useStrings } from '../lib/strings'
import './SteamAutoConfig.css'

interface SteamGame {
  appid: string
  name: string
  hasBackup: boolean
}

interface ApplyResult {
  ok: boolean
  error: string | null
  results: Record<string, { ok: boolean; error: string | null }>
  steamRelaunched: boolean
}

type Step =
  | { kind: 'idle' }
  | { kind: 'scanning' }
  | { kind: 'checklist'; games: SteamGame[]; reason: string | null; selected: Set<string> }
  | {
      kind: 'confirming'
      games: SteamGame[]
      reason: string | null
      selected: Set<string>
      mode: 'apply' | 'revert'
      appids: string[]
    }
  | { kind: 'applying'; mode: 'apply' | 'revert' }
  | { kind: 'done'; result: ApplyResult; mode: 'apply' | 'revert' }

/**
 * "Настроить автоматически" in the Steam tab of ConnectGuide. Scans for
 * installed Jackbox titles Steam already knows about (see
 * backend/bridgebox/steam_launch.py), lets the user confirm which ones to
 * touch, then closes/patches/reopens Steam through the Api bridge.
 *
 * Deliberately its own local step-machine rather than shared confirm/modal
 * plumbing - this flow's steps (scan -> checklist -> confirm -> apply ->
 * result) don't map onto the generic ConfirmDialog's single yes/no shape.
 */
export function SteamAutoConfig() {
  const strings = useStrings()
  const [step, setStep] = useState<Step>({ kind: 'idle' })

  function failed(mode: 'apply' | 'revert', err: unknown) {
    logBridgeError(err)
    setStep({
      kind: 'done',
      mode,
      result: {
        ok: false,
        error: strings.common.unexpectedError,
        results: {},
        steamRelaunched: false,
      },
    })
  }

  async function startScan() {
    setStep({ kind: 'scanning' })
    try {
      const result = await callBridge<{ ok: boolean; games: SteamGame[]; reason: string | null }>('scan_steam_games')
      const games = result.ok ? result.games : []
      setStep({ kind: 'checklist', games, reason: result.reason ?? null, selected: new Set(games.map((g) => g.appid)) })
    } catch (err) {
      // callBridge throws when the bridge itself is unavailable (dev mode,
      // or a method not yet attached) - without this, the modal would be
      // stuck on its spinner forever since setStep is never reached.
      failed('apply', err)
    }
  }

  async function runApply(appids: string[]) {
    setStep({ kind: 'applying', mode: 'apply' })
    try {
      const result = await callBridge<ApplyResult>('apply_steam_launch_options', appids)
      setStep({ kind: 'done', result, mode: 'apply' })
    } catch (err) {
      failed('apply', err)
    }
  }

  async function runRevert(appids: string[]) {
    setStep({ kind: 'applying', mode: 'revert' })
    try {
      const result = await callBridge<ApplyResult>('revert_steam_launch_options', appids)
      setStep({ kind: 'done', result, mode: 'revert' })
    } catch (err) {
      failed('revert', err)
    }
  }

  function close() {
    setStep({ kind: 'idle' })
  }

  return (
    <div className="bb-steam-auto">
      <Button variant="primary" className="bb-steam-auto__insert-btn" onClick={startScan}>
        {strings.home.steamAutoConfigButton}
      </Button>

      {step.kind === 'scanning' && (
        <Modal title={strings.home.steamAutoConfigButton} onClose={close}>
          <Spinner label={strings.home.steamAutoConfigScanning} />
        </Modal>
      )}

      {step.kind === 'checklist' && (
        <Modal title={strings.home.steamAutoConfigButton} onClose={close}>
          {step.games.length === 0 ? (
            <p className="text-body">{step.reason ?? strings.home.steamAutoConfigNoneFound}</p>
          ) : (
            <>
              <ul className="bb-steam-auto__list">
                {step.games.map((game) => (
                  <li key={game.appid} className="bb-steam-auto__row">
                    <div className="bb-steam-auto__label">
                      <Toggle
                        checked={step.selected.has(game.appid)}
                        onChange={(checked) => {
                          const selected = new Set(step.selected)
                          if (checked) selected.add(game.appid)
                          else selected.delete(game.appid)
                          setStep({ ...step, selected })
                        }}
                        label={game.name}
                      />
                      <span className="text-body">{game.name}</span>
                    </div>
                    {game.hasBackup && (
                      <Button
                        variant="ghost"
                        onClick={() =>
                          setStep({
                            kind: 'confirming',
                            games: step.games,
                            reason: step.reason,
                            selected: step.selected,
                            mode: 'revert',
                            appids: [game.appid],
                          })
                        }
                      >
                        {strings.home.steamAutoConfigRevertButton}
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
                    games: step.games,
                    reason: step.reason,
                    selected: step.selected,
                    mode: 'apply',
                    appids: [...step.selected],
                  })
                }
              >
                {strings.home.steamAutoConfigConfirmButton}
              </Button>
            </>
          )}
        </Modal>
      )}

      {step.kind === 'confirming' && (
        <Modal title={strings.home.steamAutoConfigConfirmTitle} onClose={close}>
          <p className="text-body">{strings.home.steamAutoConfigConfirmBody}</p>
          <div className="bb-steam-auto__actions">
            <Button
              variant="ghost"
              onClick={() => setStep({ kind: 'checklist', games: step.games, reason: step.reason, selected: step.selected })}
            >
              {strings.common.cancel}
            </Button>
            <Button variant="primary" onClick={() => (step.mode === 'apply' ? runApply(step.appids) : runRevert(step.appids))}>
              {strings.home.steamAutoConfigConfirmButton}
            </Button>
          </div>
        </Modal>
      )}

      {step.kind === 'applying' && (
        <Modal title={strings.home.steamAutoConfigButton} onClose={close}>
          <Spinner
            label={
              step.mode === 'apply'
                ? strings.home.steamAutoConfigApplying
                : strings.home.steamAutoConfigReverting
            }
          />
        </Modal>
      )}

      {step.kind === 'done' && (
        <Modal title={strings.home.steamAutoConfigDone} onClose={close}>
          <ul className="bb-steam-auto__list">
            {Object.entries(step.result.results).map(([appid, r]) => (
              <li key={appid} className="bb-steam-auto__row">
                {appid}: {r.ok ? '✓' : r.error}
              </li>
            ))}
          </ul>
          {step.result.error && <p className="text-body">{step.result.error}</p>}
          {!step.result.steamRelaunched && (
            <p className="text-caption">{strings.home.steamAutoConfigSteamNotRelaunched}</p>
          )}
          <Button variant="primary" onClick={close}>
            {strings.common.close}
          </Button>
        </Modal>
      )}
    </div>
  )
}
