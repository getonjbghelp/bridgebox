/**
 * A recorder for everything between "the user clicked" and "the animation
 * finished", written because four attempts at the Info screen's missing
 * entrance animation were all aimed at guesses.
 *
 * The question it exists to answer is narrow: when a screen animates in and
 * the user sees nothing, WHERE did the 320ms go? A CSS animation runs on
 * wall-clock time, so it can complete in full while the browser paints one
 * frame of it or none - and from the outside that is indistinguishable from
 * "the animation never started". Those two have opposite fixes, which is
 * exactly why guessing has not worked.
 *
 * The instrument that settles it is the pairing of two independent clocks:
 *
 *   - a setInterval sampler, which reads each running animation's own
 *     currentTime and does NOT depend on the rendering pipeline, and
 *   - a requestAnimationFrame counter, which only advances when the browser
 *     actually produced a frame.
 *
 * rAF alone cannot report the frames that never happened - if the pipeline
 * stalls, the callback simply does not run and there is nothing in the log to
 * see. Sampling on a timer instead means a stall shows up as exactly what it
 * is: the animation's currentTime marching from 0 to 280ms while the frame
 * counter never moves. That is the difference between "it did not animate"
 * and "it animated where nobody could see it", stated in data.
 *
 * ponytail: diagnostic build only. Delete this module, its import in
 * main.tsx and the marks in App.tsx once the Info screen bug is understood -
 * a permanent rAF loop plus an 8ms timer is not something to ship.
 */

const BOOT = performance.now()
const MAX_EVENTS = 8000
const MAX_FRAMES = 40000
/** Fine enough to catch a 320ms animation in ~40 samples. */
const SAMPLE_MS = 8
/** Safety net for animations whose animationstart never got dispatched. */
const DISCOVER_MS = 48
/**
 * How often to time a round trip through the pywebview bridge into the Python
 * host. This is the one probe that can leave the renderer: a long frame with
 * an IDLE main thread (no script, no style, no layout - which is exactly what
 * the Info screen trace showed) means the frame was waiting to be presented
 * rather than being computed, and the two candidates for that are the
 * compositor and the host process. If these round trips spike to ~200ms at
 * the same instant the frame counter freezes, the host was blocked and no
 * amount of frontend work can fix it. If they stay flat through the freeze,
 * the host is fine and the stall is inside the rendering pipeline.
 */
const PROBE_MS = 150

type Kind =
  | 'mark'
  | 'click'
  | 'anim-start'
  | 'anim-end'
  | 'anim-cancel'
  | 'trans-start'
  | 'trans-end'
  | 'trans-cancel'
  | 'longtask'
  | 'loaf'
  | 'visibility'
  | 'resize'
  | 'note'

type TraceEvent = {
  t: number
  kind: Kind
  what: string
  detail?: Record<string, unknown>
}

type TraceFrame = { t: number; dt: number }

/** One reading of an animation's own clock, tagged with the frame count. */
type Sample = { t: number; ct: number; frame: number }

type Watch = {
  key: string
  anim: Animation
  seenAt: number
  seenAtFrame: number
  durationMs: number
  infinite: boolean
  samples: Sample[]
  done?: number
  /**
   * The viewport and the animated box, in device pixels, as they were when
   * this animation began. Recorded because the stall turns out to track the
   * WINDOW SIZE: maximised it happens, restored down it does not. A cost that
   * scales with pixels and leaves the main thread idle is compositing work -
   * an opacity animation promotes its element to its own layer, and that
   * layer has to be created, rastered and uploaded on the animation's first
   * frame. These two numbers are what turns that from a story into a graph.
   */
  viewport: string
  boxPixels: number
}

type LoafEntry = PerformanceEntry & {
  renderStart?: number
  styleAndLayoutStart?: number
  blockingDuration?: number
  scripts?: Array<{ name?: string; duration: number; invoker?: string }>
}

/** One timed round trip out to the Python host and back. */
type Probe = { sent: number; rtt: number }

const events: TraceEvent[] = []
const frames: TraceFrame[] = []
const probes: Probe[] = []
const watching = new Map<string, Watch>()
const finished: Watch[] = []
const available: string[] = []
let probeState = 'not started'

let running = false
let frameCount = 0
let lastFrameAt = BOOT
let hiddenFor = 0

const since = (t: number) => +(t - BOOT).toFixed(1)

function push(kind: Kind, what: string, detail?: Record<string, unknown>) {
  if (events.length >= MAX_EVENTS) events.shift()
  events.push({ t: performance.now(), kind, what, detail })
}

/** Short, readable identity for an element - enough to tell screens apart. */
function label(target: EventTarget | null | undefined): string {
  if (!(target instanceof Element)) return '?'
  const cls =
    typeof target.className === 'string' ? target.className.trim().split(/\s+/)[0] : ''
  const id = target.id ? '#' + target.id : ''
  const active = target.getAttribute('data-active')
  const state = active !== null ? `[active=${active}]` : ''
  return `${target.tagName.toLowerCase()}${id}${cls ? '.' + cls : ''}${state}`
}

function animName(anim: Animation): string {
  const css = anim as CSSAnimation & CSSTransition
  return css.animationName || css.transitionProperty || anim.id || 'anim'
}

function animTarget(anim: Animation): Element | null {
  const effect = anim.effect as KeyframeEffect | null
  return effect?.target ?? null
}

function keyFor(anim: Animation): string {
  return `${label(animTarget(anim))}:${animName(anim)}`
}

/**
 * The ONLY job of the frame loop is to prove a frame happened. Everything
 * else is sampled on a timer, because a timer keeps running when the
 * rendering pipeline does not - which is the case this file exists to catch.
 */
function tick(now: number) {
  if (!running) return
  frameCount++
  if (frames.length >= MAX_FRAMES) frames.shift()
  frames.push({ t: now, dt: +(now - lastFrameAt).toFixed(1) })
  lastFrameAt = now
  requestAnimationFrame(tick)
}

function watch(anim: Animation) {
  const key = keyFor(anim)
  if (watching.has(key)) return
  const timing = anim.effect?.getComputedTiming()
  const iterations = timing?.iterations ?? 1
  const duration = typeof timing?.duration === 'number' ? timing.duration : 0
  const target = animTarget(anim)
  // getBoundingClientRect forces a style flush, so it is read once here at
  // registration - never in the per-frame sampler, which must not perturb
  // the thing it is measuring.
  const rect = target?.getBoundingClientRect()
  const dpr = window.devicePixelRatio || 1
  watching.set(key, {
    key,
    anim,
    seenAt: performance.now(),
    seenAtFrame: frameCount,
    durationMs: duration,
    infinite: !Number.isFinite(iterations),
    samples: [],
    viewport: `${window.innerWidth}x${window.innerHeight}`,
    boxPixels: rect ? Math.round(rect.width * dpr * rect.height * dpr) : 0,
  })
}

/**
 * Reading currentTime is a timeline read, not a style read - it does not
 * force a recalculation, so sampling this often does not distort what it is
 * measuring. document.getAnimations() would, which is why discovery runs on
 * its own slower interval below.
 */
function sample() {
  const now = performance.now()
  for (const w of watching.values()) {
    const ct = w.anim.currentTime
    w.samples.push({
      t: now,
      ct: typeof ct === 'number' ? +ct.toFixed(1) : -1,
      frame: frameCount,
    })
    const state = w.anim.playState
    if (state === 'finished' || state === 'idle') {
      w.done = now
      finished.push(w)
      watching.delete(w.key)
    }
  }
}

/**
 * Animation events are dispatched as part of the frame cycle, so a stalled
 * pipeline can delay or lose them entirely - which is precisely the situation
 * being investigated. Polling for animations nobody announced closes that
 * hole; the events below are kept because they carry elapsedTime and the
 * exact start/end reason, which polling cannot.
 */
function discover() {
  let all: Animation[]
  try {
    all = document.getAnimations()
  } catch {
    return
  }
  for (const anim of all) {
    if (anim.playState === 'running' || anim.playState === 'paused') watch(anim)
  }
}

/**
 * Time a round trip into the Python host. get_config is used because it is a
 * pure read - it serialises the config model and takes no runtime lock - so a
 * slow round trip means the host could not get to the call, not that the call
 * itself was expensive. Overlapping probes are skipped rather than queued: a
 * backlog would measure our own queue instead of the host.
 */
function startProbe() {
  const api = window.pywebview?.api
  if (!api || typeof api.get_config !== 'function') {
    probeState = 'unavailable (no pywebview host - plain browser dev mode)'
    return
  }
  probeState = 'running against get_config'
  let inFlight = false
  setInterval(() => {
    if (inFlight) return
    inFlight = true
    const sent = performance.now()
    void Promise.resolve(api.get_config())
      .then(() => {
        probes.push({ sent, rtt: +(performance.now() - sent).toFixed(1) })
      })
      .catch(() => {
        probeState = 'errored'
      })
      .finally(() => {
        inFlight = false
      })
  }, PROBE_MS)
}

function observe(type: string, handle: (entry: PerformanceEntry) => void): boolean {
  try {
    const obs = new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) handle(entry)
    })
    obs.observe({ type, buffered: true })
    return true
  } catch {
    // Not every engine has every entry type; a missing one is a gap in the
    // report, not a reason to lose the rest of it.
    return false
  }
}

function onAnimationEvent(kind: Kind) {
  return (raw: Event) => {
    const ev = raw as AnimationEvent
    push(kind, `${label(ev.target)}:${ev.animationName}`, {
      elapsed: +ev.elapsedTime.toFixed(3),
    })
    if (kind === 'anim-start') discover()
  }
}

function onTransitionEvent(kind: Kind) {
  return (raw: Event) => {
    const ev = raw as TransitionEvent
    push(kind, `${label(ev.target)}:${ev.propertyName}`, {
      elapsed: +ev.elapsedTime.toFixed(3),
    })
    if (kind === 'trans-start') discover()
  }
}

export function startMotionTrace() {
  if (running) return
  running = true

  requestAnimationFrame(tick)
  setInterval(sample, SAMPLE_MS)
  setInterval(discover, DISCOVER_MS)
  // The host may not have injected its API yet at module load.
  setTimeout(startProbe, 1500)

  // These bubble, so one listener at the document catches every screen, modal,
  // toggle and sidebar animation without knowing anything about them up front.
  // Capture phase, so nothing that stops propagation can hide from the trace.
  document.addEventListener('animationstart', onAnimationEvent('anim-start'), true)
  document.addEventListener('animationend', onAnimationEvent('anim-end'), true)
  document.addEventListener('animationcancel', onAnimationEvent('anim-cancel'), true)
  document.addEventListener('transitionstart', onTransitionEvent('trans-start'), true)
  document.addEventListener('transitionend', onTransitionEvent('trans-end'), true)
  document.addEventListener('transitioncancel', onTransitionEvent('trans-cancel'), true)

  document.addEventListener(
    'click',
    (e) => {
      const el = e.target as HTMLElement | null
      push('click', (el?.textContent || '').trim().slice(0, 40) || label(el))
    },
    true,
  )

  // Maximise/restore is the controlled variable in this investigation, so
  // every change of it is a landmark in the timeline rather than noise.
  let lastSize = ''
  const noteSize = () => {
    const size = `${window.innerWidth}x${window.innerHeight}`
    if (size === lastSize) return
    lastSize = size
    push('resize', size, { megapixels: +((window.innerWidth * window.innerHeight) / 1e6).toFixed(2) })
  }
  noteSize()
  window.addEventListener('resize', noteSize)

  let hiddenSince = 0
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') hiddenSince = performance.now()
    else if (hiddenSince) hiddenFor += performance.now() - hiddenSince
    push('visibility', document.visibilityState)
  })

  // Long Animation Frames say WHY a frame took 200ms - script, style/layout,
  // or render - rather than only that it did. This is what separates "the
  // screen's own first layout" from "something else was holding the thread".
  if (
    observe('long-animation-frame', (raw) => {
      const entry = raw as LoafEntry
      const endsAt = entry.startTime + entry.duration
      const detail: Record<string, unknown> = {
        at: since(entry.startTime),
        duration: Math.round(entry.duration),
        blocking: Math.round(entry.blockingDuration ?? 0),
      }
      if (entry.renderStart) detail.renderMs = Math.round(endsAt - entry.renderStart)
      if (entry.styleAndLayoutStart) {
        detail.styleLayoutMs = Math.round(endsAt - entry.styleAndLayoutStart)
      }
      // How much of the frame elapsed BEFORE any rendering work began. When
      // this is nearly the whole duration and no script ran, the frame was
      // not computing anything - it was waiting, which is the case that
      // points outside the renderer entirely.
      if (entry.renderStart) {
        detail.idleBeforeRenderMs = Math.round(entry.renderStart - entry.startTime)
      }
      const scripts = (entry.scripts ?? [])
        .filter((s) => s.duration > 4)
        .slice(0, 4)
        .map((s) => `${s.invoker || s.name || '?'} ${Math.round(s.duration)}ms`)
      if (scripts.length > 0) detail.scripts = scripts
      push('loaf', `frame ${Math.round(entry.duration)}ms`, detail)
    })
  ) {
    available.push('long-animation-frame')
  }

  if (
    observe('longtask', (entry) => {
      push('longtask', `${Math.round(entry.duration)}ms`, { start: since(entry.startTime) })
    })
  ) {
    available.push('longtask')
  }

  push('mark', 'trace armed')
}

/** Called from the app at moments the trace cannot infer on its own. */
export function traceMark(what: string, detail?: Record<string, unknown>) {
  push('mark', what, detail)
}

/**
 * The refresh interval this display actually runs at, taken from the data
 * rather than assumed to be 60Hz - "dropped frame" means nothing without it.
 */
function frameInterval(): number {
  const deltas = frames.map((f) => f.dt).filter((d) => d > 4 && d < 100)
  if (deltas.length < 20) return 16.7
  deltas.sort((a, b) => a - b)
  return +deltas[Math.floor(deltas.length / 2)].toFixed(1)
}

/**
 * Per animation, the story this file was written to tell: how far its own
 * clock had run by the time the browser first managed to draw it, and how
 * many frames of it ever reached the screen.
 */
function verdict(w: Watch, interval: number): string {
  const head =
    `  ${w.key}  @+${since(w.seenAt)}ms  (duration ${Math.round(w.durationMs)}ms${w.infinite ? ', LOOPING' : ''})` +
    `\n      window ${w.viewport}, animated box ${(w.boxPixels / 1e6).toFixed(2)}M device pixels`
  if (w.samples.length === 0) return `${head}\n      never sampled`

  // A sample counts as "drawn" once the frame counter has moved past the one
  // in effect when the animation was first seen.
  const drawn = w.samples.filter((s) => s.frame > w.seenAtFrame)
  const firstDrawn = drawn[0]
  const lastSample = w.samples[w.samples.length - 1]
  const distinctFrames = new Set(drawn.map((s) => s.frame)).size
  const life = lastSample.t - w.samples[0].t
  const possible = Math.max(1, Math.round(life / interval))

  const lines = [head]
  if (!firstDrawn) {
    lines.push(
      `      NOT ONE FRAME WAS DRAWN while it ran - its clock reached ` +
        `${Math.round(lastSample.ct)}ms with the frame counter frozen`,
    )
  } else {
    const lost = Math.round(firstDrawn.ct)
    const pct = w.durationMs > 0 ? Math.round((lost / w.durationMs) * 100) : 0
    const bad = !w.infinite && (pct > 15 || distinctFrames < possible * 0.6)
    lines[0] = head + (bad ? '   <== PROBLEM' : '')
    lines.push(
      `      first frame drawn when its clock already read ${lost}ms` +
        (w.durationMs > 0 ? ` (${pct}% of the animation already gone)` : ''),
    )
    lines.push(
      `      ${distinctFrames} frames drawn over ${Math.round(life)}ms, ~${possible} were possible`,
    )
  }
  // Thin the sample list so a long animation stays readable.
  const step = Math.max(1, Math.floor(w.samples.length / 24))
  const shown = w.samples.filter((_, i) => i % step === 0)
  lines.push(
    `      clock/frame: ${shown.map((s) => `${Math.round(s.ct)}@f${s.frame}`).join(' ')}`,
  )
  return lines.join('\n')
}

export function motionTraceReport(): string {
  const interval = frameInterval()
  const root = document.documentElement
  const lines: string[] = []
  const elapsed = performance.now() - BOOT
  const expectedFrames = Math.round((elapsed - hiddenFor) / interval)

  lines.push('=== BridgeBox motion trace ===')
  lines.push(`ua: ${navigator.userAgent}`)
  lines.push(
    `cores: ${navigator.hardwareConcurrency ?? '?'} | dpr: ${window.devicePixelRatio} | ` +
      `window: ${window.innerWidth}x${window.innerHeight}`,
  )
  lines.push(
    `frame interval: ${interval}ms (~${Math.round(1000 / interval)}Hz) | ` +
      `frames drawn: ${frameCount} of ~${expectedFrames} possible in ${Math.round(elapsed)}ms`,
  )
  lines.push(
    `data-animations: ${root.getAttribute('data-animations') ?? 'unset'} | ` +
      `prefers-reduced-motion: ` +
      `${matchMedia('(prefers-reduced-motion: reduce)').matches ? 'reduce' : 'no-preference'}`,
  )
  lines.push(`observers available: ${available.join(', ') || 'none'}`)
  lines.push(`dumped at boot+${Math.round(elapsed)}ms`)
  if (expectedFrames > 40 && frameCount < expectedFrames * 0.25) {
    lines.push(
      'WARNING: the frame counter barely moved for the whole session. If the ' +
        'window was minimised or in the background, this trace is not usable - ' +
        'reproduce with the window visible and in the foreground.',
    )
  }

  lines.push('')
  lines.push('--- ANIMATION VERDICTS (what actually reached the screen) ---')
  const all = [...finished, ...watching.values()].sort((a, b) => a.seenAt - b.seenAt)
  const entrances = all.filter((w) => !w.infinite)
  const loops = all.filter((w) => w.infinite)
  lines.push(
    entrances.length > 0
      ? entrances.map((w) => verdict(w, interval)).join('\n')
      : '  (no finite animations recorded)',
  )
  if (loops.length > 0) {
    lines.push('')
    lines.push('  looping animations (spinners - listed for completeness only):')
    lines.push(loops.map((w) => `    ${w.key} @+${since(w.seenAt)}ms`).join('\n'))
  }

  lines.push('')
  lines.push('--- SCREEN SIZES (is any screen simply too big to paint?) ---')
  for (const el of document.querySelectorAll('.bb-app__screen')) {
    const heading = el.querySelector('h1')?.textContent?.trim() || '?'
    lines.push(
      `  ${heading}: ${el.querySelectorAll('*').length} elements, ` +
        `${el.querySelectorAll('svg').length} svg, ` +
        `${el.querySelectorAll('img').length} img, ` +
        `${el.innerHTML.length} chars of markup`,
    )
  }

  lines.push('')
  lines.push('--- HOST ROUND TRIPS (is the Python side blocking the window?) ---')
  lines.push(`  probe: ${probeState}, ${probes.length} samples`)
  if (probes.length > 0) {
    const rtts = probes.map((p) => p.rtt).sort((a, b) => a - b)
    const median = rtts[Math.floor(rtts.length / 2)]
    lines.push(
      `  round trip median ${median}ms, worst ${rtts[rtts.length - 1]}ms ` +
        `(a healthy host answers in single-digit ms)`,
    )
    const slow = [...probes].sort((a, b) => b.rtt - a.rtt).slice(0, 8)
    lines.push(`  slowest: ${slow.map((p) => `${p.rtt}ms @+${since(p.sent)}`).join(', ')}`)
  }

  lines.push('')
  lines.push('--- WORST FRAME GAPS ---')
  const worst = [...frames]
    .filter((f) => f.dt > interval * 1.8)
    .sort((a, b) => b.dt - a.dt)
    .slice(0, 25)
  if (worst.length === 0) {
    lines.push('  (none - every frame arrived on time)')
  } else {
    // The correlation that decides it: what the host was doing around each
    // freeze. A round trip that spikes in step with the gap means the window
    // stalled because the host could not answer; one that stays flat means
    // the host was fine and the stall was inside the renderer.
    for (const f of worst) {
      const near = probes.filter((p) => p.sent > f.t - f.dt - 300 && p.sent < f.t + 300)
      const hostPeak = near.reduce((m, p) => (p.rtt > m ? p.rtt : m), 0)
      lines.push(
        `  +${since(f.t)}ms  gap ${Math.round(f.dt)}ms  ` +
          `| host round trips nearby: ${near.length > 0 ? near.map((p) => Math.round(p.rtt) + 'ms').join(' ') : 'none sampled'}` +
          (hostPeak > 80 ? '   <== HOST WAS BLOCKED TOO' : ''),
      )
    }
  }

  lines.push('')
  lines.push('--- TIMELINE ---')
  for (const e of events) {
    const detail = e.detail ? ' ' + JSON.stringify(e.detail) : ''
    lines.push(`  +${since(e.t)}ms  [${e.kind}] ${e.what}${detail}`)
  }

  return lines.join('\n')
}

/**
 * A badge, not only a hotkey: the point is that someone who is not watching a
 * console can reproduce the bug and hand the result over.
 */
function installBadge() {
  const badge = document.createElement('button')
  badge.type = 'button'
  badge.textContent = 'TRACE'
  badge.style.cssText = [
    'position:fixed',
    'right:8px',
    'bottom:8px',
    'z-index:2147483647',
    'font:11px/1.4 ui-monospace,monospace',
    'padding:4px 8px',
    'border-radius:6px',
    'border:1px solid #f5a524',
    'background:#1b1200',
    'color:#f5a524',
    'cursor:pointer',
    'opacity:0.75',
  ].join(';')
  badge.title = 'Скопировать трассировку (Ctrl+Alt+D)'

  const copy = async () => {
    const text = motionTraceReport()
    // Console first: if the clipboard is unavailable in this WebView, the
    // report still exists somewhere retrievable.
    console.log(text)
    try {
      await navigator.clipboard.writeText(text)
      badge.dataset.state = 'СКОПИРОВАНО'
    } catch {
      badge.dataset.state = 'СМ. КОНСОЛЬ'
    }
    badge.textContent = badge.dataset.state
    setTimeout(() => {
      delete badge.dataset.state
      badge.textContent = 'TRACE'
    }, 2000)
  }

  badge.addEventListener('click', copy)
  window.addEventListener('keydown', (e) => {
    if (e.ctrlKey && e.altKey && e.code === 'KeyD') {
      e.preventDefault()
      void copy()
    }
  })

  const attach = () => document.body.appendChild(badge)
  if (document.body) attach()
  else document.addEventListener('DOMContentLoaded', attach)

  setInterval(() => {
    if (!badge.dataset.state) badge.textContent = `TRACE ${frameCount}f`
  }, 1000)
}

export function installMotionTrace() {
  startMotionTrace()
  installBadge()
  ;(window as unknown as Record<string, unknown>).__bbTrace = {
    report: motionTraceReport,
    mark: traceMark,
    events,
    frames,
  }
}
