import type { MutableRefObject } from 'react'

/** Clears a window.setInterval id stored in a ref and resets the ref to
 *  undefined - the shared teardown every polling loop in this app needs,
 *  both between runs and on unmount. */
export function clearPoll(ref: MutableRefObject<number | undefined>) {
  if (ref.current !== undefined) {
    window.clearInterval(ref.current)
    ref.current = undefined
  }
}
