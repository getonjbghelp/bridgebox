import { memo, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Button } from '../components/Button'
import { useStrings, t } from '../lib/strings'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { callBridge, isNativeBridgeAvailable, logBridgeError, waitForBridgeReady } from '../lib/bridge'
import './LogsScreen.css'

type Level = 'debug' | 'info' | 'warning' | 'error'

interface LogLine {
  seq: number
  time: number
  // Whatever levelname the backend sent, not just the four we have pills for
  // - see levelOf.
  level: string
  logger: string
  message: string
  // Call site, so a line points at the code that produced it.
  module?: string
  func?: string
  line?: number
  thread?: string
  traceback?: string
}

interface LogLinesResponse {
  ok: boolean
  lines: LogLine[]
  nextSeq: number
}

// One label source for both the filter pills and the per-line badge (which
// used to read line.level.toUpperCase() straight off the backend value) -
// otherwise editing a level's text in strings.json would desync them.
function levelLabels(strings: ReturnType<typeof useStrings>): Record<Level, string> {
  return {
    debug: strings.logs.levelDebug,
    info: strings.logs.levelInfo,
    warning: strings.logs.levelWarning,
    error: strings.logs.levelError,
  }
}

const LEVELS: Level[] = ['debug', 'info', 'warning', 'error']
const KNOWN_LEVELS = new Set<string>(LEVELS)

// Python has levels these pills can't express - logger.critical() arrives as
// "critical", and a custom level as whatever it was named. Bucketing them
// into error beats dropping them: an unmatched level would fail every pill's
// filter, so the most severe lines in the log were the ones silently hidden.
function levelOf(line: LogLine): Level {
  return KNOWN_LEVELS.has(line.level) ? (line.level as Level) : 'error'
}

function formatTime(epochSeconds: number, locale: 'ru' | 'en'): string {
  return new Date(epochSeconds * 1000).toLocaleTimeString(locale === 'ru' ? 'ru-RU' : 'en-US')
}

// Rounding in scrollTop means "at the bottom" is never exactly 0 - a few px of
// slack keeps auto-follow from switching itself off on its own scroll.
const AT_BOTTOM_SLACK_PX = 24

// Matches log_buffer.EXPORT_FORMATS. .log for pasting into a chat, .json for a
// script, .html for opening. The backend renders all three so the exported
// file and the file log say the same thing in the same shape.
function exportFormats(strings: ReturnType<typeof useStrings>) {
  return [
    { fmt: 'log', label: strings.logs.exportLog },
    { fmt: 'json', label: strings.logs.exportJson },
    { fmt: 'html', label: strings.logs.exportHtml },
  ] as const
}

// A log is append-only: every retained line's data never changes after it
// arrives, only new ones get added. Without this memo, the poll tick that
// appends a handful of new lines re-rendered and re-diffed every line ever
// kept (up to 3000) once a second - the actual cost on this screen once
// logging.level=debug turns a minute of gameplay into thousands of lines.
// React.memo skips exactly the rows whose props (all primitives, or `line`
// itself, which is only ever appended - never mutated) are unchanged.
const LogRow = memo(function LogRow({
  line,
  label,
  locale,
  threadSeparator,
  traceSummary,
}: {
  line: LogLine
  label: string
  locale: 'ru' | 'en'
  threadSeparator: string
  traceSummary: string
}) {
  return (
    <div className="bb-logs__line">
      <span className="text-mono bb-logs__time">{formatTime(line.time, locale)}</span>
      <span className={`bb-logs__badge bb-logs__badge--${levelOf(line)}`}>{label}</span>
      <span className="bb-logs__body">
        <span className="text-mono bb-logs__message">{line.message}</span>
        {line.module && (
          <span className="text-mono bb-logs__origin">
            {line.module}.{line.func}:{line.line}
            {line.thread && line.thread !== 'MainThread' && (
              <>
                {threadSeparator}
                {line.thread}
              </>
            )}
          </span>
        )}
        {line.traceback && (
          <details className="bb-logs__trace">
            <summary>{traceSummary}</summary>
            <pre className="text-mono">{line.traceback}</pre>
          </details>
        )}
      </span>
    </div>
  )
})

export function LogsScreen({ active }: { active: boolean }) {
  const strings = useStrings()
  const { locale } = useMotionPrefs()
  const LEVEL_LABEL = levelLabels(strings)
  const EXPORT_FORMATS = exportFormats(strings)
  const [lines, setLines] = useState<LogLine[]>([])
  const [activeLevels, setActiveLevels] = useState<Set<Level>>(
    new Set(['debug', 'info', 'warning', 'error']),
  )
  const [search, setSearch] = useState('')
  const nextSeq = useRef(0)
  const viewport = useRef<HTMLDivElement>(null)
  const [following, setFollowing] = useState(true)
  const [exporting, setExporting] = useState<string | null>(null)
  const [exportNote, setExportNote] = useState<string | null>(null)

  // The screen stays mounted while another one is on top (see App.tsx), so
  // without this gate the app would keep making one IPC call per second for a
  // list nobody is looking at. Resuming picks up from nextSeq, so nothing that
  // happened while away is lost - the backend buffer kept it.
  useEffect(() => {
    if (!active) return
    let cancelled = false
    let id: number | undefined

    async function poll() {
      const result = await callBridge<LogLinesResponse>(
        'get_log_lines',
        nextSeq.current,
        500,
      )
      if (cancelled || !result.ok) return
      if (result.lines.length > 0) {
        setLines((prev) => [...prev, ...result.lines].slice(-3000))
        nextSeq.current = result.nextSeq
      }
    }

    waitForBridgeReady().then(() => {
      if (cancelled || !isNativeBridgeAvailable()) return
      poll()
      id = window.setInterval(poll, 1000)
    })

    return () => {
      cancelled = true
      if (id !== undefined) window.clearInterval(id)
    }
  }, [active])

  function toggleLevel(level: Level) {
    setActiveLevels((prev) => {
      const next = new Set(prev)
      if (next.has(level)) next.delete(level)
      else next.add(level)
      return next
    })
  }

  // Pin to the newest line while the user is at the bottom, and stop the
  // moment they scroll up to read something - a log that yanks itself away
  // mid-read is worse than one that never follows at all. Layout effect, so
  // the jump happens in the same frame the rows are painted.
  useLayoutEffect(() => {
    const el = viewport.current
    if (el && following) el.scrollTop = el.scrollHeight
  })

  function handleScroll() {
    const el = viewport.current
    if (!el) return
    setFollowing(el.scrollHeight - el.scrollTop - el.clientHeight <= AT_BOTTOM_SLACK_PX)
  }

  const filtered = useMemo(() => {
    const query = search.trim().toLowerCase()
    return lines.filter((line) => {
      if (!activeLevels.has(levelOf(line))) return false
      if (query === '') return true
      // Searching by function or module name is the fast way to find every
      // line a given piece of code emitted.
      const haystack = [line.message, line.logger, line.module, line.func, line.traceback]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      return haystack.includes(query)
    })
  }, [lines, activeLevels, search])

  function copyAll() {
    // Copied text keeps the call site and stack - that is what gets pasted
    // into a bug report.
    const text = filtered
      .map((l) => {
        const origin = l.module ? ` (${l.module}.${l.func}:${l.line})` : ''
        const trace = l.traceback ? `\n${l.traceback.trimEnd()}` : ''
        return `[${formatTime(l.time, locale)}] ${l.level.toUpperCase()} ${l.logger}${origin} ${l.message}${trace}`
      })
      .join('\n')
    navigator.clipboard?.writeText(text)
  }

  // Exports the WHOLE buffer, not `filtered`. An export is for a bug report,
  // and one missing whatever the level pills happened to be hiding is worse
  // than useless - so the backend reads its own buffer rather than taking a
  // copy of what is on screen.
  async function exportLogs(fmt: string) {
    if (!isNativeBridgeAvailable()) return
    setExporting(fmt)
    setExportNote(null)
    try {
      const result = await callBridge<{ ok: boolean; error: string | null; path: string }>(
        'export_logs',
        fmt,
      )
      if (!result.ok) {
        setExportNote(t(strings.logs.exportFailed, { error: result.error ?? '' }))
      } else if (result.path) {
        setExportNote(t(strings.logs.exportDone, { path: result.path }))
      }
    } catch (err) {
      logBridgeError(err)
      setExportNote(t(strings.logs.exportFailed, { error: strings.common.unexpectedError }))
    } finally {
      setExporting(null)
    }
  }

  return (
    <div className="bb-logs">
      <h1 className="text-display" style={{ marginBottom: 'var(--space-5)' }}>
        {strings.logs.title}
      </h1>

      <div className="bb-logs__toolbar">
        <div className="bb-logs__levels">
          {LEVELS.map((key) => (
            <button
              key={key}
              type="button"
              className={`bb-logs__level bb-logs__level--${key}${
                activeLevels.has(key) ? ' bb-logs__level--active' : ''
              }`}
              onClick={() => toggleLevel(key)}
            >
              {LEVEL_LABEL[key]}
            </button>
          ))}
        </div>
        <div className="bb-logs__actions">
          <input
            className="bb-input"
            placeholder={strings.logs.searchPlaceholder}
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Button variant="ghost" onClick={copyAll}>
            {strings.logs.copyButton}
          </Button>
          <Button variant="ghost" onClick={() => setLines([])}>
            {strings.logs.clearButton}
          </Button>
        </div>
      </div>

      <div className="bb-logs__export">
        <span className="text-caption">{strings.logs.exportLabel}</span>
        {EXPORT_FORMATS.map(({ fmt, label }) => (
          <Button
            key={fmt}
            variant="ghost"
            onClick={() => exportLogs(fmt)}
            disabled={exporting !== null}
          >
            {label}
          </Button>
        ))}
        {exportNote && <span className="bb-logs__export-note text-caption">{exportNote}</span>}
      </div>

      <div className="bb-logs__stream">
        <div className="bb-logs__viewport" ref={viewport} onScroll={handleScroll}>
          {filtered.map((line) => (
            <LogRow
              key={line.seq}
              line={line}
              label={LEVEL_LABEL[levelOf(line)]}
              locale={locale}
              threadSeparator={strings.logs.threadSeparator}
              traceSummary={strings.logs.traceSummary}
            />
          ))}
          {filtered.length === 0 && <p className="text-caption">{strings.logs.emptyState}</p>}
        </div>
        {!following && (
          <button className="bb-logs__follow" type="button" onClick={() => setFollowing(true)}>
            {strings.logs.followResume}
          </button>
        )}
      </div>
    </div>
  )
}
