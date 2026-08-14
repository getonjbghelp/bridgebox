// Backs the "don't ask again" checkbox on ConfirmDialog. localStorage, not
// config.yaml: this is a per-machine UI nicety, not a setting the user would
// ever want to inspect or sync, so it doesn't belong in the same file the
// rest of Settings persists to.
const KEY_PREFIX = 'bb-skip-confirm:'

export function isConfirmSkipped(id: string): boolean {
  try {
    return localStorage.getItem(KEY_PREFIX + id) === '1'
  } catch {
    return false
  }
}

export function setConfirmSkipped(id: string): void {
  try {
    localStorage.setItem(KEY_PREFIX + id, '1')
  } catch {
    // Storage can be unavailable (private mode, quota, disabled) - the
    // checkbox is a convenience, not a guarantee, so a failure to persist it
    // must not block the action the user already confirmed.
  }
}
