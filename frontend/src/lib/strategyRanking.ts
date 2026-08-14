// Collapsing a strategy-suite run into one row per strategy, for the setup
// wizard's «Выбрана стратегия».
//
// Its own module rather than a helper inside SetupWizard.tsx so it can be
// imported by `node --test`, which cannot load a .tsx that pulls in React and
// a stylesheet. Settings deliberately does NOT use this - there the two
// protocols' tables side by side are the point.

export type TargetSet = 'ecast' | 'blobcast'

export interface TargetResult {
  ok: boolean
  elapsedMs: number | null
  status: number | null
  error: string | null
}

export interface StrategyResult {
  key: string
  name: string
  ok: boolean
  error: string | null
  targets: Record<string, TargetResult>
  targetSet: TargetSet
}

export interface Aggregate {
  key: string
  name: string
  ok: boolean
  ms: number | null
}

/** Fastest responding target in one measured row, or null if none answered. */
function bestTargetMs(row: StrategyResult): number | null {
  const times = Object.values(row.targets ?? {})
    .filter((target) => target.ok && typeof target.elapsedMs === 'number')
    .map((target) => target.elapsedMs as number)
  return times.length > 0 ? Math.min(...times) : null
}

/**
 * One row per strategy, not per (strategy, stage) pair.
 *
 * A "both" run reports every strategy twice - once per protocol - because the
 * suite makes two complete passes. For the wizard the only question is "does
 * this one work", so the passes collapse into a row that is `ok` only if
 * *every* pass answered, timed by the slowest of them. A strategy that is
 * quick on Ecast and dead on Blobcast is not a good default for someone who
 * has not picked a Party Pack yet, and averaging the two would hide that.
 *
 * Sorted working-first, then ascending by time.
 */
export function aggregateResults(results: StrategyResult[]): Aggregate[] {
  const byKey = new Map<string, Aggregate>()
  for (const row of results) {
    const ms = bestTargetMs(row)
    const rowOk = row.ok && ms !== null
    const seen = byKey.get(row.key)
    if (!seen) {
      byKey.set(row.key, { key: row.key, name: row.name, ok: rowOk, ms })
      continue
    }
    seen.ok = seen.ok && rowOk
    seen.ms = seen.ms === null || ms === null ? null : Math.max(seen.ms, ms)
  }
  return [...byKey.values()].sort((a, b) => {
    if (a.ok !== b.ok) return a.ok ? -1 : 1
    if (a.ms === null || b.ms === null) return a.ms === null ? 1 : -1
    return a.ms - b.ms
  })
}
