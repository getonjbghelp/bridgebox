import { useCallback, useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { BrandLogo } from '../components/BrandLogo'
import { Button } from '../components/Button'
import { Spinner } from '../components/Spinner'
import { Toggle } from '../components/Toggle'
import { Segmented } from '../components/Segmented'
import {
  IconCheck,
  IconCheckCircle,
  IconCloudSync,
  IconCopy,
  IconLock,
  IconRadar,
  IconSteam,
} from '../components/icons'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { useSpringTransition, useStepTransition } from '../lib/motion'
import { aggregateResults, type StrategyResult, type TargetSet } from '../lib/strategyRanking'
import { useStrings, t, strategyGroupLabel } from '../lib/strings'
import { callBridge, isNativeBridgeAvailable, waitForBridgeReady } from '../lib/bridge'
import './SetupWizard.css'

/**
 * First-run setup. Shown instead of the whole app - sidebar included - while
 * `ui.setup_complete` is false, which is the shipped state (no config.yaml)
 * and the state a factory reset returns to.
 *
 * Full-screen rather than a modal over the app on purpose: a modal implies
 * something behind it you could get to, and one of these steps genuinely
 * cannot be skipped. There is nothing behind this yet.
 */

const STEPS = ['welcome', 'certificate', 'strategy', 'updates', 'connect', 'done'] as const

const STRATEGY_POLL_MS = 700
const FALLBACK_HOST = '127.0.0.1'
const FALLBACK_PORT = 8443

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
  stage: TargetSet | null
  done: boolean
}

interface WizardConfig {
  server: { host: string; port: number }
  update: { check_on_startup: boolean }
  zapret: { strategy: string }
}

interface StrategyOption {
  key: string
  name: string
}
type StrategyGroups = Record<string, StrategyOption[]>

interface StrategiesResponse {
  ok: boolean
  groups: StrategyGroups
}

export function SetupWizard() {
  const strings = useStrings()
  const {
    theme,
    setTheme,
    animationsEnabled,
    setAnimationsEnabled,
    language,
    setLanguage,
    setSetupComplete,
  } = useMotionPrefs()
  const stepTransition = useStepTransition()
  const transition = useSpringTransition('default')

  const [index, setIndex] = useState(0)
  const [direction, setDirection] = useState(1)
  const [config, setConfig] = useState<WizardConfig | null>(null)

  // -- certificate step ----------------------------------------------------
  const [certState, setCertState] = useState<'idle' | 'running' | 'done' | 'error'>('idle')
  const [certError, setCertError] = useState<string | null>(null)

  // -- strategy step -------------------------------------------------------
  const [strategyState, setStrategyState] = useState<'idle' | 'running' | 'done' | 'error'>('idle')
  const [strategyResults, setStrategyResults] = useState<StrategyResult[]>([])
  const [strategyTotal, setStrategyTotal] = useState(0)
  const [strategyStage, setStrategyStage] = useState<TargetSet | null>(null)
  const [strategyError, setStrategyError] = useState<string | null>(null)
  const [chosenStrategy, setChosenStrategy] = useState<string | null>(null)
  const [strategyGroups, setStrategyGroups] = useState<StrategyGroups>({})
  const [manualPicking, setManualPicking] = useState(false)
  const [manualStrategy, setManualStrategy] = useState('general')
  const pollRef = useRef<number | undefined>(undefined)

  // -- updates step --------------------------------------------------------
  const [checkOnStartup, setCheckOnStartup] = useState(false)

  // -- connect step --------------------------------------------------------
  const [copied, setCopied] = useState(false)

  const step = STEPS[index]
  const host = config?.server.host ?? FALLBACK_HOST
  const port = config?.server.port ?? FALLBACK_PORT
  const address = `${host}:${port}`
  // Same string HomeScreen builds - the game prepends the scheme itself and
  // rejects a serverUrl that already carries one.
  const launchOption = `-jbg.config serverUrl=${address}`

  // A first run is dark, per the wizard's own default. Written through
  // setTheme so it persists like any other theme change.
  //
  // Guarded by a ref rather than an empty dependency list: setTheme is a new
  // closure on every provider render, so listing it would re-fire this and
  // pin the toggle to dark - the opposite of offering a choice - while
  // omitting it is the lint warning that hides exactly that class of bug.
  const appliedDefaultTheme = useRef(false)
  useEffect(() => {
    if (appliedDefaultTheme.current) return
    appliedDefaultTheme.current = true
    setTheme('dark')
  }, [setTheme])

  useEffect(() => {
    waitForBridgeReady().then(() => {
      if (!isNativeBridgeAvailable()) return
      callBridge<{ ok: boolean; config: WizardConfig | null }>('get_config')
        .then((result) => {
          if (!result.ok || !result.config) return
          setConfig(result.config)
          setCheckOnStartup(result.config.update.check_on_startup)
        })
        .catch(() => {})
      // The CA may already be trusted - a factory reset clears the config but
      // not the Windows certificate store, and re-running certutil for a cert
      // that is already there would ask for nothing and prove nothing.
      callBridge<{ certInstalled: boolean }>('bridge_status')
        .then((status) => {
          if (status.certInstalled) setCertState('done')
        })
        .catch(() => {})
      // For "Выбрать из списка" - the same call Settings' strategy dropdown
      // uses, fetched once up front rather than only when the button is
      // clicked, so choosing it feels instant instead of waiting on a round
      // trip.
      callBridge<StrategiesResponse>('list_strategies')
        .then((result) => {
          if (result.ok) setStrategyGroups(result.groups)
        })
        .catch(() => {})
    })
  }, [])

  const stopPolling = useCallback(() => {
    if (pollRef.current !== undefined) {
      window.clearInterval(pollRef.current)
      pollRef.current = undefined
    }
  }, [])

  // Leaving the wizard mid-suite used to be the same bug Settings had: the
  // backend keeps cycling winws through every remaining strategy with nothing
  // on screen, and stops on whichever one it happened to reach.
  useEffect(() => {
    return () => {
      stopPolling()
      if (isNativeBridgeAvailable()) callBridge('test_strategies_cancel').catch(() => {})
    }
  }, [stopPolling])

  function go(next: number) {
    setDirection(next > index ? 1 : -1)
    setIndex(next)
  }

  const canLeaveCertStep = certState === 'done'

  function goNext() {
    if (index < STEPS.length - 1) go(index + 1)
  }

  function goBack() {
    if (index > 0) go(index - 1)
  }

  async function installCertificate() {
    setCertState('running')
    setCertError(null)
    if (!isNativeBridgeAvailable()) {
      window.setTimeout(() => setCertState('done'), 900)
      return
    }
    try {
      const result = await callBridge<{ ok: boolean; error: string | null }>('install_certificate')
      setCertState(result.ok ? 'done' : 'error')
      setCertError(result.ok ? null : result.error)
    } catch (err) {
      setCertState('error')
      setCertError(String(err))
    }
  }

  async function runStrategyTest() {
    setStrategyState('running')
    setStrategyResults([])
    setStrategyTotal(0)
    setStrategyError(null)
    setChosenStrategy(null)
    setStrategyStage('ecast')
    if (!isNativeBridgeAvailable()) {
      window.setTimeout(() => setStrategyState('done'), 1200)
      return
    }

    // skip_heavy=true, target_set="both": the wizard wants a strategy that
    // works for both protocols, and the "Прочие" group is slow enough to
    // probe that including it here would double a wait already measured in
    // minutes. Settings is where someone can run the exhaustive version.
    // skip_heavy=false: every strategy on disk, including the «Прочие» group
    // (Fake TLS Auto*, Simple Fake*). They are slow to probe, which is why
    // Settings still defaults to skipping them - but this runs once, and a
    // first-run user whose only working strategy lives in that group would
    // otherwise be told nothing works.
    const started = await callBridge<TestStrategiesStart>('test_strategies', false, 'both')
    if (!started.ok) {
      setStrategyState('error')
      setStrategyError(started.error)
      return
    }
    setStrategyTotal(started.total)

    stopPolling()
    pollRef.current = window.setInterval(async () => {
      try {
        const progress = await callBridge<TestStrategiesProgress>('test_strategies_progress')
        setStrategyResults(progress.results ?? [])
        if (progress.stage) setStrategyStage(progress.stage)
        if (progress.done) {
          stopPolling()
          setStrategyStage(null)
          setStrategyState(progress.error ? 'error' : 'done')
          setStrategyError(progress.error)
          // Deliberately NOT progress.fastestKey. The backend ranks by the
          // fastest single target (`min` across a row's targets); this list
          // ranks by the slowest stage (`max`), because that is the latency
          // the player actually waits through. Two different metrics meant the
          // highlighted row was routinely not the one with the smallest number
          // next to it - which reads as a random pick. One metric now, and it
          // is the one on screen.
          const winner = aggregateResults(progress.results ?? []).find((row) => row.ok)?.key
          if (winner) applyStrategy(winner)
        }
      } catch (err) {
        stopPolling()
        setStrategyStage(null)
        setStrategyState('error')
        setStrategyError(String(err))
      }
    }, STRATEGY_POLL_MS)
  }

  function cancelStrategyTest() {
    stopPolling()
    setStrategyState('idle')
    setStrategyStage(null)
    if (isNativeBridgeAvailable()) callBridge('test_strategies_cancel').catch(() => {})
  }

  function applyStrategy(key: string) {
    setChosenStrategy(key)
    if (isNativeBridgeAvailable()) {
      callBridge('update_config', { zapret: { strategy: key } }).catch(() => {})
    }
  }

  // "Выбрать из списка" - skips the ping test entirely for somebody who
  // already knows what works, or just wants through setup faster. Reuses the
  // done-state UI below by setting strategyState directly, rather than a
  // fourth state parallel to idle/running/done/error - it IS "done", the
  // suite just never ran.
  function confirmManualStrategy() {
    applyStrategy(manualStrategy)
    setStrategyState('done')
  }

  function setUpdatePolicy(value: boolean) {
    setCheckOnStartup(value)
    if (isNativeBridgeAvailable()) {
      callBridge('update_config', { update: { check_on_startup: value } }).catch(() => {})
    }
  }

  function copyLaunchOption() {
    navigator.clipboard?.writeText(launchOption)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1600)
  }

  const ranked = aggregateResults(strategyResults)
  // Falls back through the manual picker's own list when nothing was ever
  // tested (`ranked` is empty on that path) - without it, a manual pick
  // showed its raw key ("general") instead of the name Settings' dropdown
  // shows for the same strategy.
  const chosenName =
    ranked.find((row) => row.key === chosenStrategy)?.name ??
    Object.values(strategyGroups)
      .flat()
      .find((opt) => opt.key === chosenStrategy)?.name ??
    chosenStrategy

  return (
    <div className="bb-setup">
      <div className="bb-setup__stage">
        <AnimatePresence mode="wait" initial={false}>
          {/* Only the entrance is directional. Doing it with `variants` +
              AnimatePresence's `custom` - so the leaving panel could slide the
              other way on Back - hangs the exit outright: variants propagate
              to descendant motion components, and the panel's own children
              include Toggle's `layout` span, whose exit never reports done.
              The result was a wizard whose step counter advanced while the
              content stayed on the previous step. Which way the outgoing panel
              leaves was never load-bearing anyway; the incoming one carries
              the sense of forward or back on its own. */}
          <motion.div
            key={step}
            className="bb-setup__panel"
            initial={{ opacity: 0, x: direction * 24 }}
            animate={{ opacity: 1, x: 0, transition: stepTransition.in }}
            exit={{ opacity: 0, transition: stepTransition.out }}
          >
            {step === 'welcome' && (
              <StepLayout
                // The same "bb" the rail shows when collapsed - the first
                // thing a new user sees should be the mark they will be
                // looking at from then on, not a second one.
                icon={<BrandLogo title={strings.sidebar.brandName} monogram />}
                title={strings.setup.welcomeTitle}
                body={strings.setup.welcomeBody}
              >
                <div className="bb-setup__prefs">
                  <PrefRow
                    label={strings.setup.languageLabel}
                    control={
                      <Segmented
                        value={language}
                        onChange={setLanguage}
                        ariaLabel={strings.setup.languageLabel}
                        options={[
                          { value: 'system', label: strings.settings.languageSystem },
                          { value: 'ru', label: 'RU' },
                          { value: 'en', label: 'EN' },
                        ]}
                      />
                    }
                  />
                  <PrefRow
                    label={strings.setup.welcomeThemeLabel}
                    control={
                      <Toggle
                        checked={theme === 'dark'}
                        onChange={(value) => setTheme(value ? 'dark' : 'light')}
                        label={strings.setup.welcomeThemeLabel}
                      />
                    }
                  />
                  <PrefRow
                    label={strings.setup.welcomeAnimationsLabel}
                    hint={strings.setup.welcomeAnimationsHint}
                    control={
                      <Toggle
                        checked={animationsEnabled}
                        onChange={setAnimationsEnabled}
                        label={strings.setup.welcomeAnimationsLabel}
                      />
                    }
                  />
                </div>

                <Actions>
                  <Button variant="primary" fullWidth onClick={goNext}>
                    {strings.setup.welcomeStartButton}
                  </Button>
                  {/* No "skip everything" here. The certificate step cannot be
                      skipped at all, so a global skip could only ever stop
                      there - a button that promises to skip the whole wizard
                      and then refuses is worse than no button. Skipping lives
                      on the individual steps that genuinely allow it. */}
                </Actions>
              </StepLayout>
            )}

            {step === 'certificate' && (
              <StepLayout
                icon={
                  <span
                    className={`bb-setup__lock${certState === 'done' ? ' bb-setup__lock--closed' : ''}`}
                  >
                    <IconLock size={40} closed={certState === 'done'} />
                  </span>
                }
                title={strings.setup.certTitle}
                body={strings.setup.certBody}
                tone={certState === 'done' ? 'success' : 'default'}
              >
                <p className="text-caption bb-setup__note">{strings.setup.certWarning}</p>

                {certState === 'done' ? (
                  <p className="bb-setup__ok">
                    <IconCheck size={18} />
                    {strings.setup.certInstalledLabel}
                  </p>
                ) : (
                  <Button
                    variant="primary"
                    fullWidth
                    disabled={certState === 'running'}
                    onClick={installCertificate}
                  >
                    {certState === 'running' ? (
                      <>
                        <Spinner size={15} />
                        {strings.setup.certInstallingButton}
                      </>
                    ) : certState === 'error' ? (
                      strings.setup.certRetryButton
                    ) : (
                      strings.setup.certInstallButton
                    )}
                  </Button>
                )}
                {certError && <p className="text-caption bb-setup__error">{certError}</p>}

                <Actions>
                  <Button
                    variant={certState === 'done' ? 'primary' : 'secondary'}
                    fullWidth
                    disabled={!canLeaveCertStep}
                    onClick={goNext}
                  >
                    {strings.setup.nextButton}
                  </Button>
                  {/* Says why «Далее» is dead, in place, before it is pressed.
                      This used to be a modal that only appeared once somebody
                      tried to skip past - an explanation you have to trigger
                      is one most people never read. */}
                  {!canLeaveCertStep && (
                    <p className="text-caption bb-setup__skip-warning">
                      {strings.setup.certRequiredBody}
                    </p>
                  )}
                </Actions>
              </StepLayout>
            )}

            {step === 'strategy' && (
              <StepLayout
                icon={
                  <span
                    className={`bb-setup__radar${strategyState === 'running' ? ' bb-setup__radar--scanning' : ''}`}
                  >
                    <IconRadar size={40} />
                  </span>
                }
                title={strings.setup.strategyTitle}
                body={strings.setup.strategyBody}
              >
                {strategyState === 'idle' && !manualPicking && (
                  <>
                    <p className="text-caption bb-setup__note">{strings.setup.strategyHint}</p>
                    <div className="bb-setup__button-stack">
                      <Button variant="primary" fullWidth onClick={runStrategyTest}>
                        {strings.setup.strategyRunButton}
                      </Button>
                      <Button variant="ghost" fullWidth onClick={() => setManualPicking(true)}>
                        {strings.setup.strategyPickManualButton}
                      </Button>
                    </div>
                  </>
                )}

                {strategyState === 'idle' && manualPicking && (
                  <div className="bb-setup__manual-pick">
                    <p className="text-caption bb-setup__note">{strings.setup.strategyManualHint}</p>
                    <select
                      className="bb-select"
                      value={manualStrategy}
                      onChange={(e) => setManualStrategy(e.target.value)}
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
                    <Button variant="primary" fullWidth onClick={confirmManualStrategy}>
                      {strings.setup.strategyManualConfirmButton}
                    </Button>
                    <Button variant="ghost" fullWidth onClick={() => setManualPicking(false)}>
                      {strings.common.cancel}
                    </Button>
                  </div>
                )}

                {strategyState === 'running' && (
                  <div className="bb-setup__progress">
                    <div className="bb-setup__progress-head">
                      <span className="text-subtitle">{strings.setup.strategyRunningTitle}</span>
                      <span className="text-numeric bb-setup__percent">
                        {strategyTotal > 0
                          ? `${Math.min(99, Math.round((strategyResults.length / strategyTotal) * 100))}%`
                          : '…'}
                      </span>
                    </div>
                    <div
                      className="bb-setup__bar"
                      role="progressbar"
                      aria-valuemin={0}
                      aria-valuemax={strategyTotal || undefined}
                      aria-valuenow={strategyTotal ? strategyResults.length : undefined}
                    >
                      <motion.span
                        className="bb-setup__bar-fill"
                        // Width, not scaleX: a scaled fill would stretch the
                        // rounded cap into an ellipse at every value but 100%.
                        animate={{
                          width: strategyTotal
                            ? `${(strategyResults.length / strategyTotal) * 100}%`
                            : '8%',
                        }}
                        transition={transition}
                      />
                    </div>
                    <p className="text-caption">
                      {strategyTotal > 0
                        ? t(strings.setup.strategyProgressWithTotal, {
                            done: strategyResults.length,
                            total: strategyTotal,
                          })
                        : strings.setup.strategyProgressNoTotal}
                      {strategyStage && (
                        <>
                          {' · '}
                          {t(strings.setup.strategyStageLabel, {
                            stage:
                              strategyStage === 'ecast'
                                ? strings.settings.profilesKindEcast
                                : strings.settings.profilesKindBlobcast,
                          })}
                        </>
                      )}
                    </p>
                    <Button variant="ghost" fullWidth onClick={cancelStrategyTest}>
                      {strings.setup.strategyCancelButton}
                    </Button>
                  </div>
                )}

                {(strategyState === 'done' || strategyState === 'error') && (
                  <>
                    {chosenStrategy ? (
                      <>
                        <p className="bb-setup__ok">
                          <IconCheck size={18} />
                          {strings.setup.strategyPickedLabel}: <strong>{chosenName}</strong>
                        </p>
                        <p className="text-caption bb-setup__note">
                          {strings.setup.strategyPickHint}
                        </p>
                      </>
                    ) : (
                      <p className="text-caption bb-setup__note">
                        {strings.setup.strategyNoneWorked}
                      </p>
                    )}

                    {ranked.length > 0 && (
                      <ul className="bb-setup__results">
                        {ranked.map((row) => (
                          <li key={row.key}>
                            <button
                              type="button"
                              className="bb-setup__result"
                              aria-pressed={row.key === chosenStrategy}
                              disabled={!row.ok}
                              onClick={() => applyStrategy(row.key)}
                            >
                              <span className="bb-setup__result-name">{row.name}</span>
                              <span
                                className={`text-numeric bb-setup__result-ms${row.ok ? '' : ' bb-setup__result-ms--bad'}`}
                              >
                                {/* Whole milliseconds. The probe returns a
                                    float, and "722.3599000826297 мс" is 13
                                    digits of noise on a number nobody compares
                                    below single-millisecond resolution. */}
                                {row.ok && row.ms !== null
                                  ? `${Math.round(row.ms)} ${strings.settings.msUnit}`
                                  : strings.setup.strategyFailedCell}
                              </span>
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}

                    {strategyError && (
                      <p className="text-caption bb-setup__error">{strategyError}</p>
                    )}

                    <Button variant="secondary" fullWidth onClick={runStrategyTest}>
                      {strings.setup.strategyRetryButton}
                    </Button>
                  </>
                )}

                <Actions>
                  {/* Secondary until a strategy is actually picked, same rule
                      as the certificate step. As a disabled primary it was a
                      second blue full-width button stacked under «Запустить
                      авто-тест» - two accents on one screen, and the dimmed
                      one still reading as the thing to press. */}
                  <Button
                    variant={chosenStrategy === null ? 'secondary' : 'primary'}
                    fullWidth
                    disabled={chosenStrategy === null}
                    onClick={goNext}
                  >
                    {strings.setup.nextButton}
                  </Button>
                  {/* Both gone once a strategy is actually chosen: there is
                      nothing left to skip, and the warning would be claiming
                      the default is still in play when it no longer is.
                      The consequence sits above the button rather than behind
                      a confirm - a warning you have to trigger to read is one
                      most people click past without reading. */}
                  {chosenStrategy === null && !manualPicking && (
                    <>
                      <p className="text-caption bb-setup__skip-warning">
                        {strings.setup.strategySkipWarning}
                      </p>
                      <Button
                        variant="ghost"
                        fullWidth
                        onClick={() => {
                          cancelStrategyTest()
                          goNext()
                        }}
                      >
                        {strings.setup.strategySkipButton}
                      </Button>
                    </>
                  )}
                </Actions>
              </StepLayout>
            )}

            {step === 'updates' && (
              <StepLayout
                icon={<IconCloudSync size={40} />}
                title={strings.setup.updatesTitle}
                body={strings.setup.updatesBody}
              >
                <div className="bb-setup__choices">
                  <ChoiceCard
                    selected={checkOnStartup}
                    label={strings.setup.updatesAutoLabel}
                    hint={strings.setup.updatesAutoHint}
                    onSelect={() => setUpdatePolicy(true)}
                  />
                  <ChoiceCard
                    selected={!checkOnStartup}
                    label={strings.setup.updatesManualLabel}
                    hint={strings.setup.updatesManualHint}
                    onSelect={() => setUpdatePolicy(false)}
                  />
                </div>

                <Actions>
                  <Button variant="primary" fullWidth onClick={goNext}>
                    {strings.setup.nextButton}
                  </Button>
                </Actions>
              </StepLayout>
            )}

            {step === 'connect' && (
              <StepLayout
                // The Steam mark rather than a second copy glyph 40px above
                // the copy button. Steam is the first route the body names,
                // and the footnote below points at the full guide that covers
                // standalone copies with their own icon.
                icon={<IconSteam size={38} />}
                title={strings.setup.connectTitle}
                body={strings.setup.connectBody}
              >
                <code className="text-mono bb-setup__code">{launchOption}</code>
                <p className="text-caption bb-setup__note">{strings.setup.connectNoScheme}</p>

                <Button variant="secondary" fullWidth onClick={copyLaunchOption}>
                  {copied ? (
                    <>
                      <IconCheck size={16} />
                      {strings.setup.connectCopiedButton}
                    </>
                  ) : (
                    <>
                      <IconCopy size={16} />
                      {strings.setup.connectCopyButton}
                    </>
                  )}
                </Button>

                {/* Its own class, not .bb-setup__note: that one is 40ch wide
                    with a 16px bottom margin, which broke this longer sentence
                    into four ragged lines and then stacked its margin on top
                    of .bb-setup__actions' own 24px. */}
                <p className="text-caption bb-setup__footnote">{strings.setup.connectFootnote}</p>

                <Actions>
                  <Button variant="primary" fullWidth onClick={goNext}>
                    {strings.setup.connectNextButton}
                  </Button>
                </Actions>
              </StepLayout>
            )}

            {step === 'done' && (
              <StepLayout
                icon={
                  <span className="bb-setup__burst">
                    <IconCheckCircle size={44} />
                    {/* Eight sparks on a ring, each rotated into place by an
                        index-derived angle - a dependency-free stand-in for
                        confetti, and one that respects the animations gate
                        because it is plain CSS. */}
                    {Array.from({ length: 8 }, (_, i) => (
                      <span
                        key={i}
                        className="bb-setup__spark"
                        style={{ '--spark-angle': `${i * 45}deg` } as CSSProperties}
                      />
                    ))}
                  </span>
                }
                title={strings.setup.doneTitle}
                body={strings.setup.doneBody}
                tone="success"
              >
                <Actions>
                  <Button variant="primary" fullWidth onClick={() => setSetupComplete(true)}>
                    {strings.setup.doneButton}
                  </Button>
                </Actions>
              </StepLayout>
            )}
          </motion.div>
        </AnimatePresence>
      </div>

      <div className="bb-setup__footer">
        {/* Always rendered, hidden with visibility rather than removed.
            Unmounting it made the footer shorter on step 1 than on step 2, and
            since the stage takes the remaining height, that shortfall moved
            the whole panel by 13px the moment you pressed «Начать настройку» -
            the jump this was blamed on the panel for. visibility:hidden keeps
            the box, and still takes it out of the tab order and the a11y
            tree. */}
        <button
          type="button"
          className="bb-setup__back"
          data-hidden={index === 0 || index === STEPS.length - 1}
          onClick={goBack}
        >
          {strings.setup.backButton}
        </button>

        <ol
          className="bb-setup__dots"
          aria-label={t(strings.setup.progressAriaLabel, {
            current: index + 1,
            total: STEPS.length,
          })}
        >
          {STEPS.map((id, i) => (
            <li
              key={id}
              className="bb-setup__dot"
              data-state={i === index ? 'current' : i < index ? 'done' : 'todo'}
              aria-current={i === index ? 'step' : undefined}
            />
          ))}
        </ol>

        <span />
      </div>

    </div>
  )
}

function StepLayout({
  icon,
  title,
  body,
  tone = 'default',
  children,
}: {
  icon: ReactNode
  title: string
  body: string
  tone?: 'default' | 'success'
  children: ReactNode
}) {
  return (
    <>
      <div className={`bb-setup__icon bb-setup__icon--${tone}`}>{icon}</div>
      <h1 className="text-display bb-setup__title">{title}</h1>
      <p className="text-body bb-setup__body">{body}</p>
      {children}
    </>
  )
}

/** The bottom-anchored button stack. Its own element so every step's actions
 *  land at the same distance from the panel edge regardless of how much
 *  content sits above them. */
function Actions({ children }: { children: ReactNode }) {
  return <div className="bb-setup__actions">{children}</div>
}

function PrefRow({
  label,
  hint,
  control,
}: {
  label: string
  hint?: string
  control: ReactNode
}) {
  return (
    <div className="bb-setup__pref">
      <span className="bb-setup__pref-text">
        <span className="text-body bb-setup__pref-label">{label}</span>
        {hint && <span className="text-caption">{hint}</span>}
      </span>
      {control}
    </div>
  )
}

function ChoiceCard({
  selected,
  label,
  hint,
  onSelect,
}: {
  selected: boolean
  label: string
  hint: string
  onSelect: () => void
}) {
  return (
    <button
      type="button"
      className="bb-setup__choice"
      role="radio"
      aria-checked={selected}
      onClick={onSelect}
    >
      <span className="bb-setup__choice-mark" aria-hidden="true" />
      <span className="bb-setup__choice-text">
        <span className="text-body bb-setup__choice-label">{label}</span>
        <span className="text-caption">{hint}</span>
      </span>
    </button>
  )
}
