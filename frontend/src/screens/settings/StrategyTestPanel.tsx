import { useEffect, useRef, useState } from 'react'
import { AnimatePresence } from 'framer-motion'
import { Row } from '../../components/Section'
import { Toggle } from '../../components/Toggle'
import { Button } from '../../components/Button'
import { Modal } from '../../components/Modal'
import { DiagBadge, type DiagState } from '../../components/DiagBadge'
import { IconClose } from '../../components/icons'
import { useStrings, t } from '../../lib/strings'
import { callBridge, isNativeBridgeAvailable, logBridgeError } from '../../lib/bridge'
import { clearPoll } from '../../lib/poll'
import type { TargetResult, TargetSet, StrategyResult, TestStrategiesStart, TestStrategiesProgress } from './types'

const STRATEGY_POLL_MS = 700

/** "Ecast"/"Blobcast" - reusing the same protocol-name strings the profile
 *  kind picker already shows, rather than a second copy of the same two
 *  words under a different key. */
function stageLabel(stage: TargetSet, strings: ReturnType<typeof useStrings>): string {
  return stage === 'ecast' ? strings.settings.profilesKindEcast : strings.settings.profilesKindBlobcast
}

/** One row per strategy instead of one per (strategy, stage) pair - a "both"
 *  run reports every strategy twice (see backend's _stages_for), once per
 *  protocol, because the suite makes two complete passes with disjoint
 *  target hosts. Ecast's pass always finishes first (see _stages_for's
 *  ordering), so a strategy's row is seen with its Ecast targets before its
 *  Blobcast one arrives - merging keeps that column order without having to
 *  sort it separately. `ok` is ANDed across passes: a strategy that is quick
 *  on Ecast and dead on Blobcast should not read as fully working. */
function mergeStagePasses(results: StrategyResult[]): StrategyResult[] {
  const byKey = new Map<string, StrategyResult>()
  for (const row of results) {
    const merged = byKey.get(row.key)
    if (!merged) {
      byKey.set(row.key, { ...row, targets: { ...row.targets } })
      continue
    }
    merged.ok = merged.ok && row.ok
    Object.assign(merged.targets, row.targets)
  }
  return [...byKey.values()]
}

/** Column names read off the (already merged) rows themselves rather than
 *  hardcoded - unioned across every row, not just the first, because a
 *  strategy that fails outright before ever reaching a target carries an
 *  empty targets object and would otherwise hide real columns. */
function targetColumns(rows: StrategyResult[]): string[] {
  const seen = new Set<string>()
  for (const row of rows) {
    for (const name of Object.keys(row.targets ?? {})) seen.add(name)
  }
  return [...seen]
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

/** A hook rather than a component: the trigger row lives inside the Network
 *  tab's conditional block, but the results popup must NOT unmount when the
 *  user switches to another tab - the suite runs for minutes in the
 *  background, and losing the popup would leave it running uncancelled with
 *  no way back to it. A component would tie both to the same mount point;
 *  splitting the return into `trigger` (rendered inside the tab) and `modal`
 *  (rendered once, unconditionally, alongside the screen's other modals)
 *  keeps them on one shared state without a context provider for it. */
export function useStrategyTestPanel(onApply: (key: string) => void) {
  const strings = useStrings()

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
  // Which stage is running right now, mirrored from progress.stage - drives
  // the "Этап: Ecast/Blobcast" label so a run that always covers both
  // protocols doesn't look stuck during the (much longer) second half.
  const [runningStage, setRunningStage] = useState<TargetSet | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const pollRef = useRef<number | undefined>(undefined)

  // Leaving the screen mid-run must not keep polling a dead component.
  useEffect(() => () => stopPolling(), [])

  function stopPolling() {
    clearPoll(pollRef)
  }

  async function runStrategyTest() {
    setStrategyTest('running')
    setDiagError(null)
    setExportError(null)
    setStrategyResults([])
    setFastestKey(null)
    setStrategyTotal(0)
    setRunningStage('ecast')
    setStrategyPopupOpen(true)
    if (!isNativeBridgeAvailable()) {
      window.setTimeout(() => setStrategyTest('done'), 1200)
      return
    }

    // The suite runs for minutes, so it's a background job on the backend:
    // start it, then poll. Waiting on one long call is what previously let a
    // timeout throw away every result that had already been measured.
    // Always "both": the popup has room for two tables now (see the
    // SettingsScreen tab split), so there's no reason to make someone pick a
    // protocol and run the suite twice to see the whole picture.
    const started = await callBridge<TestStrategiesStart>('test_strategies', !testHeavy, 'both')
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
        logBridgeError(err)
        stopPolling()
        setRunningStage(null)
        setStrategyTest('error')
        setDiagError(strings.common.unexpectedError)
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
    onApply(key)
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

  // One merged row per strategy (Ecast + Blobcast targets together), not one
  // table per protocol - see mergeStagePasses.
  const strategyRows = mergeStagePasses(strategyResults)
  const strategyColumns = targetColumns(strategyRows)

  const trigger = (
    <>
      <Row
        label={strings.settings.strategyTestLabel}
        hint={strings.settings.strategyTestHint}
        control={
          <div className="bb-diag">
            <Button variant="secondary" onClick={runStrategyTest} disabled={strategyTest === 'running'}>
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
        control={
          <Toggle checked={testHeavy} onChange={setTestHeavy} label={strings.settings.testHeavyLabel} />
        }
      />
      {diagError && !strategyPopupOpen && <p className="text-caption bb-diag__error">{diagError}</p>}
    </>
  )

  const modal = (
    <AnimatePresence>
      {strategyPopupOpen && (
        <Modal title={strings.settings.strategyModalTitle} onClose={closeStrategyPopup} maxWidth={820}>
          {/* Wider than the other modals on this screen: one merged table
              now carries all three target hosts (two Ecast, one Blobcast)
              as columns side by side, and 680px was cramped enough to wrap
              even "General" onto two lines. */}
          {strategyTest === 'running' && (
            <p className="text-body">
              {strategyTotal > 0
                ? t(strings.settings.strategyModalProgressWithTotal, {
                    done: strategyResults.length,
                    total: strategyTotal,
                  })
                : strings.settings.strategyModalProgressNoTotal}
              {/* Every run covers both protocols now, so this is worth
                  stating on every run, not just a former "both" mode. */}
              {runningStage && (
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
          {strategyRows.length > 0 && (
            <div
              className="bb-strategy-table"
              style={{ gridTemplateColumns: `1.4fr repeat(${strategyColumns.length}, 1fr) auto` }}
            >
              <div className="bb-strategy-table__row bb-strategy-table__row--head">
                <span>{strings.settings.strategyTableHeaderName}</span>
                {strategyColumns.map((name) => (
                  <span key={name}>{name}</span>
                ))}
                <span></span>
              </div>
              {strategyRows.map((r) => (
                <div key={r.key} className="bb-strategy-table__row">
                  <span>
                    {r.name}
                    {r.key === fastestKey && (
                      <span className="bb-strategy-table__badge">
                        {strings.settings.strategyTableFastestBadge}
                      </span>
                    )}
                  </span>
                  {strategyColumns.map((name) => (
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
          )}
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
  )

  return { trigger, modal }
}
