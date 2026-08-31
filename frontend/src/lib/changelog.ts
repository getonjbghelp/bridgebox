// Explicit .ts extensions (allowImportingTsExtensions in tsconfig): these are
// real, non-type imports, and node --test resolves the module graph with
// Node's own strict ESM loader rather than Vite's bundler - it needs a full
// specifier to find a sibling module the way a browser build does not.
import type { ChangeLevel, ChangelogEntry } from './content.ts'
import { LEGACY_CHANGELOG } from './content.ts'
import { pickReleaseNotes } from './releaseNotes.ts'
import { callBridge, isNativeBridgeAvailable, waitForBridgeReady } from './bridge.ts'

/**
 * The Info screen's "История версий" - GitHub Releases, not a local file.
 *
 * Every release from 0.1.6 onward carries its own bilingual title and
 * severity directly in its GitHub release body, one line per language:
 *
 *   «Название» • MINOR/MAJOR/CRITICAL
 *
 * (quotes: any of «» "" '' “” ‘’ - a typo in which pair was typed should
 * never be the reason a release's name fails to show up). That marker line
 * is the first line of each language's own section - the RU/EN split
 * itself is releaseNotes.ts's job, already built for the app-update banner
 * and reused here rather than duplicated.
 *
 * Everything OLDER than 0.1.6 predates that convention - those titles were
 * written by hand into changelog.json (now legacyChangelog.json) before
 * this existed, and are never going to gain a new entry, so they are just
 * appended after whatever GitHub returns rather than merged into it. GitHub
 * still lists those same old releases; CUTOFF is what keeps them from
 * appearing twice - LEGACY_CHANGELOG's own hand-written copy is the only
 * one shown for any version older than it.
 */

/** Below this, defer to LEGACY_CHANGELOG - see the module comment above. */
const CUTOFF = [0, 1, 6]

export interface GithubRelease {
  version: string
  name: string
  body: string
  date: string
  htmlUrl: string
}

interface ChangelogResponse {
  ok: boolean
  error: string | null
  releases: GithubRelease[]
}

/** "v0.1.6b1" -> [0, 1, 6]. Same trimming rule as the backend's
 * app_update._numeric_parts: stops at the first non-numeric run, which is
 * what drops a pre-release suffix. Unparseable input is [] - callers treat
 * that as "not comparable", never as 0. */
function numericParts(version: string): number[] {
  const match = version.replace(/^[vV]/, '').match(/\d+(?:\.\d+)*/)
  if (!match) return []
  return match[0].split('.').map(Number)
}

/** Exported for its own test coverage - see extractMarker/toChangelogEntry
 * below for the same reasoning. */
export function isAtLeast(version: string, cutoff: number[]): boolean {
  const parts = numericParts(version)
  if (parts.length === 0) return false
  for (let i = 0; i < Math.max(parts.length, cutoff.length); i++) {
    const a = parts[i] ?? 0
    const b = cutoff[i] ?? 0
    if (a !== b) return a > b
  }
  return true
}

// Any of the quote conventions a maintainer might reach for, straight or
// curly, in either language - see the module comment on why this is
// deliberately permissive.
const TITLE_RE = /^[«"'“‘]([^»"'”’]+)[»"'”’]\s*•\s*(MINOR|MAJOR|CRITICAL)\s*$/i

/**
 * Pulls the "«Название» • LEVEL" marker off the first non-blank line of one
 * language's section, if it is there. When it is not - a release published
 * before the author had the convention in mind, or one that just forgot -
 * this is not an error: the whole section becomes the body, and the caller
 * falls back to the release's own name for a title and 'minor' for a level,
 * exactly as if nothing had been written for that field at all.
 */
export function extractMarker(text: string): { title: string | null; level: ChangeLevel | null; body: string } {
  const lines = text.split('\n')
  let i = 0
  while (i < lines.length && lines[i].trim() === '') i++
  if (i < lines.length) {
    const match = TITLE_RE.exec(lines[i].trim())
    if (match) {
      return {
        title: match[1].trim(),
        level: match[2].toLowerCase() as ChangeLevel,
        body: lines.slice(i + 1).join('\n').trim(),
      }
    }
  }
  return { title: null, level: null, body: text.trim() }
}

export function toChangelogEntry(release: GithubRelease): ChangelogEntry {
  const ru = extractMarker(pickReleaseNotes(release.body, 'ru'))
  const en = extractMarker(pickReleaseNotes(release.body, 'en'))
  // Whichever language's marker actually carried a level wins - a genuine
  // disagreement should never happen (one release, one severity), so this
  // only ever matters when just one side follows the convention.
  const level: ChangeLevel = ru.level ?? en.level ?? 'minor'
  return {
    version: release.version,
    date: release.date,
    level,
    ru: { title: ru.title ?? release.name, body: ru.body },
    en: { title: en.title ?? release.name, body: en.body },
  }
}

/**
 * Newest first: whatever GitHub returns for 0.1.6+ (already sorted that
 * way), followed by the frozen legacy block (also already sorted that way -
 * see legacyChangelog.json). Throws on a network/bridge failure rather than
 * silently returning the legacy tail alone - BetaBadge decides how to show
 * that failure and which fallback to render, this module just reports it.
 */
export async function fetchChangelog(): Promise<ChangelogEntry[]> {
  await waitForBridgeReady()
  if (!isNativeBridgeAvailable()) {
    throw new Error('pywebview bridge unavailable (dev mode) - method: changelog')
  }
  const result = await callBridge<ChangelogResponse>('changelog')
  if (!result.ok) throw new Error(result.error ?? 'changelog fetch failed')

  const fresh = result.releases.filter((r) => isAtLeast(r.version, CUTOFF)).map(toChangelogEntry)
  return [...fresh, ...LEGACY_CHANGELOG]
}
