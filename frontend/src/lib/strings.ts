import ru from '../data/strings/ru.json'
import en from '../data/strings/en.json'
import { useMotionPrefs } from '../state/MotionPrefsContext'

// ru.json is the source of truth for shape; en.json must satisfy the exact
// same sections and keys or this fails to compile - the same guarantee the
// old single-file strings.json gave for free, now spread across two files.
const CATALOGS = { ru, en } satisfies Record<'ru' | 'en', typeof ru>

/** The active locale's full catalog, re-rendered wherever the language changes. */
export function useStrings() {
  const { locale } = useMotionPrefs()
  return CATALOGS[locale]
}

/** Fills {placeholders} in a strings catalog value: t(strings.home.zapretUpdateAvailable, { latest }). */
export function t(template: string, vars?: Record<string, string | number>): string {
  if (!vars) return template
  return template.replace(/\{(\w+)\}/g, (match, key) =>
    key in vars ? String(vars[key]) : match,
  )
}

/** zapret/strategies.py's GroupName ("Основная"/"Альтернативы"/"Прочие") is a
 * backend-internal identifier, not UI text - list_strategies() sends it
 * as-is regardless of UiConfig.language, same as any other data key. This is
 * the one place that turns it into a label, for the <optgroup> in Settings'
 * and the wizard's strategy dropdowns. An unrecognised group (should the
 * backend ever add one) falls back to the raw name rather than disappearing. */
export function strategyGroupLabel(group: string, strings: ReturnType<typeof useStrings>): string {
  switch (group) {
    case 'Основная':
      return strings.settings.strategyGroupMain
    case 'Альтернативы':
      return strings.settings.strategyGroupAlternatives
    case 'Прочие':
      return strings.settings.strategyGroupOther
    default:
      return group
  }
}
