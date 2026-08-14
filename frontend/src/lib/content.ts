import changelogData from '../data/content/changelog.json'
import aboutData from '../data/content/about.json'
import type { Locale } from '../state/MotionPrefsContext'

/** How important a release was - decides the changelog badge's colour. */
export type ChangeLevel = 'minor' | 'major' | 'critical'

interface ChangelogText {
  title: string
  body: string
}

export interface ChangelogEntry {
  /** Matches app_info().label, or the full version. */
  version: string
  /** ISO date, formatted for display. */
  date: string
  level: ChangeLevel
  ru: ChangelogText
  en: ChangelogText
}

/** New entries go at the TOP - see tools/build_content.py, which is what
 * actually writes here; this file is not meant to be hand-edited. */
export const CHANGELOG = changelogData as ChangelogEntry[]

/**
 * A link button on the Info screen: an icon with a hover hint (`label`),
 * which either opens `url` directly or shows a popup with `popupTitle` /
 * `popupText` (markdown-lite, same grammar as renderRich) and an optional
 * secondary link inside the popup. Exactly one of the two is ever read -
 * which one is `action`'s job, not a presence check on the fields, so a
 * link mid-edit in build_content.py can't silently do the wrong thing.
 */
export interface AboutLink {
  id: string
  /** A key into components/icons.tsx's LINK_ICONS registry, or the literal
   * 'custom' - in which case `iconSvg` holds the markup instead. */
  icon: string
  /** Raw `<svg>...</svg>` markup, imported through build_content.py's icon
   * uploader. Only present when `icon === 'custom'`; validated there
   * (starts with `<svg`, no `<script>`, no inline event handlers) since
   * that tool is the only thing that ever writes it. */
  iconSvg?: string
  label: Record<Locale, string>
  action: 'link' | 'popup'
  url?: string
  popupTitle?: Record<Locale, string>
  popupText?: Record<Locale, string>
  popupUrl?: string
  popupUrlLabel?: Record<Locale, string>
}

interface AboutLocaleContent {
  description: string
  license: { name: string; text: string }
}

interface AboutData {
  ru: AboutLocaleContent
  en: AboutLocaleContent
  links: AboutLink[]
}

export const ABOUT = aboutData as AboutData

/** A translation not written yet reads as Russian rather than blank - the
 * same fallback the strings.json build enforces at compile time, applied
 * here at runtime since a locale-content file has no such check. */
export function changelogText(entry: ChangelogEntry, locale: Locale): ChangelogText {
  return entry[locale] ?? entry.ru
}

export function aboutText(locale: Locale): AboutLocaleContent {
  return ABOUT[locale] ?? ABOUT.ru
}

/** Same RU-fallback rule as changelogText/aboutText, for the per-locale
 * records inside an AboutLink (label, popupTitle, popupText, popupUrlLabel). */
export function localeText(record: Record<Locale, string> | undefined, locale: Locale): string {
  return record?.[locale] ?? record?.ru ?? ''
}
