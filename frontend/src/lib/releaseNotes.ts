import type { Locale } from '../state/MotionPrefsContext'

/**
 * Picks one language out of a GitHub release body.
 *
 * BridgeBox releases are published bilingually in a single body, with one
 * language folded into a <details> block so the release page shows the other
 * one open. That is fine on GitHub and wrong in the app: the update modal used
 * to render the whole body, so a user saw the release twice - once in each
 * language - with the literal tags `<details>` and `<summary>English Version`
 * printed above it, because the changelog renderer only understands Markdown
 * and passes HTML straight through as text.
 *
 * Pure and JSX-free on purpose, same reasoning as lib/linkTokens.ts: the
 * project's `node --test` runner cannot import anything with JSX in it, and
 * this is the part with the branching worth testing.
 */

interface Section {
  /** Which language this block is, or null when nothing labels it. */
  locale: Locale | null
  text: string
}

const DETAILS_RE = /<details\b[^>]*>([\s\S]*?)<\/details>/gi
const SUMMARY_RE = /<summary\b[^>]*>([\s\S]*?)<\/summary>/i

/**
 * Reads the language off a <summary> label. Matched on the language's own
 * name in either script, so it keeps working whichever language ends up
 * being the folded one - the maintainer writes "English Version" today, but
 * "Русская версия" would classify just as well.
 */
function localeOfLabel(label: string): Locale | null {
  if (/english/i.test(label) || /англ/i.test(label)) return 'en'
  if (/russian/i.test(label) || /рус/i.test(label)) return 'ru'
  return null
}

/** Splits a release body into <details> blocks and the prose between them. */
export function splitReleaseNotes(body: string): Section[] {
  const sections: Section[] = []
  const re = new RegExp(DETAILS_RE.source, DETAILS_RE.flags)
  let lastIndex = 0
  let match: RegExpExecArray | null

  while ((match = re.exec(body))) {
    const before = body.slice(lastIndex, match.index).trim()
    if (before) sections.push({ locale: null, text: before })

    const summary = SUMMARY_RE.exec(match[1])
    sections.push({
      locale: localeOfLabel(summary ? summary[1] : ''),
      // The tags themselves must not survive: the renderer downstream would
      // print them as body text.
      text: match[1].replace(SUMMARY_RE, '').trim(),
    })
    lastIndex = match.index + match[0].length
  }

  const tail = body.slice(lastIndex).trim()
  if (tail) sections.push({ locale: null, text: tail })
  return sections
}

/**
 * The release notes to show a user reading the app in `locale`.
 *
 * Never returns nothing when the body had something in it. A release with no
 * language markers at all (a single-language release, or a maintainer who
 * dropped the convention) is shown in full rather than suppressed - being
 * shown the wrong language beats being told a release has no notes.
 */
export function pickReleaseNotes(body: string, locale: Locale): string {
  const sections = splitReleaseNotes(body)
  const join = (parts: Section[]) => parts.map((s) => s.text).filter(Boolean).join('\n\n')

  const labelled = sections.filter((s) => s.locale !== null)
  if (labelled.length === 0) return join(sections)

  const exact = sections.filter((s) => s.locale === locale)
  if (exact.length) return join(exact)

  // Some other language is folded away, so whatever was left unlabelled is
  // this one - that is exactly the shape the current releases use, where only
  // the English half is wrapped and the Russian half is plain body text.
  const unlabelled = sections.filter((s) => s.locale === null)
  if (unlabelled.length) return join(unlabelled)

  // Every block is labelled and none of them is ours. Show what there is.
  return join(labelled)
}
