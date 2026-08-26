import changelogData from '../data/content/changelog.json'
import aboutData from '../data/content/about.json'
import peopleData from '../data/content/people.json'
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
 * records inside an AboutLink (label, popupTitle, popupText, popupUrlLabel)
 * and a Donator/BugHunter/Tester's locale fields. `||` rather than `??`
 * deliberately - a locale field that exists but is still an empty string
 * (a bulk-imported PEOPLE entry that has not been translated to English
 * yet, see tools/build_content.py) must fall back exactly the same as a
 * missing key would, or an untranslated entry renders blank in English
 * instead of showing the Russian that is actually there. */
export function localeText(record: Record<Locale, string> | undefined, locale: Locale): string {
  return record?.[locale] || record?.ru || ''
}

/**
 * The "Спасибо" people module: donators, bug hunters, and testers - distinct
 * from CREDITS in data/credits.ts, which attributes the open-source projects
 * BridgeBox is built on, not the people who supported this one. Written by
 * tools/build_content.py, same as CHANGELOG/ABOUT above.
 */
interface PersonBase {
  id: string
  name: string
  /** Optional avatar image URL. Absent renders the pill with initials only. */
  avatar?: string
}

export interface Donator extends PersonBase {
  /** ISO date, displayed as-is - not translated. */
  date: string
  /** Free text: "Donatty", "Boosty", "USDT", ... */
  platform: string
  /** Free text tier/amount label, e.g. "500 ₽" - optional since some donors
   * prefer not to disclose the amount. */
  amount?: string
  comment?: Record<Locale, string>
}

export interface BugHunter extends PersonBase {
  bugTitle: Record<Locale, string>
  bugDescription: Record<Locale, string>
  /** Link to the GitHub Issue or commit that credits them, if any. */
  link?: string
}

export interface Tester extends PersonBase {
  tested: Record<Locale, string>
  /** Free text: OS/build/config, e.g. "Windows 10 22H2". */
  environment: string
  contribution: Record<Locale, string>
}

/** A thank-you that isn't a donation, a bug report, or testing - an idea, art,
 * moral support, anything worth naming that the other three categories don't
 * fit. Deliberately one free-text field: forcing it into testers/bughunters'
 * shape would misdescribe whatever it actually was. */
export interface Other extends PersonBase {
  reason: Record<Locale, string>
}

export interface PeopleData {
  donators: Donator[]
  bughunters: BugHunter[]
  testers: Tester[]
  other: Other[]
}

export const PEOPLE = peopleData as PeopleData
