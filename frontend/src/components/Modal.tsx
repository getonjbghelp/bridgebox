import { motion } from 'framer-motion'
import { useEffect, useId, useRef, type ReactNode } from 'react'
import { useSpringTransition } from '../lib/motion'
import { useStrings } from '../lib/strings'
import { IconClose } from './icons'
import './Modal.css'

interface ModalProps {
  title: string
  onClose: () => void
  children: ReactNode
  maxWidth?: number
}

/**
 * Built on native <dialog> + showModal(), which is a deletion rather than an
 * addition: the focus trap, Escape handling, inert background and the
 * backdrop all come from the platform. The previous hand-rolled scrim had
 * none of them - no focus trap, no Escape, no role/aria-modal, no scroll
 * lock. WebView2 is Chromium, so support is not in question.
 */
export function Modal({ title, onClose, children, maxWidth = 480 }: ModalProps) {
  const strings = useStrings()
  const transition = useSpringTransition()
  const ref = useRef<HTMLDialogElement>(null)
  const titleId = useId()

  useEffect(() => {
    const dialog = ref.current
    if (!dialog) return

    if (!dialog.open) dialog.showModal()
    // <dialog> makes the background inert but does not stop it scrolling.
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'

    return () => {
      document.body.style.overflow = previousOverflow
      if (dialog.open) dialog.close()
    }
  }, [])

  return (
    <dialog
      ref={ref}
      className="bb-modal-dialog"
      aria-labelledby={titleId}
      // Escape fires `cancel`; preventing the native close lets React drive
      // the unmount instead, so AnimatePresence still gets to play the exit.
      onCancel={(event) => {
        event.preventDefault()
        onClose()
      }}
      // Clicking outside the panel closes it. Two targets count, because the
      // area outside the panel is covered by two things: the dialog's own box
      // (where ::backdrop used to be the only thing under the pointer) and the
      // scrim below, which is a real element and therefore what a click
      // actually lands on. Checking only the dialog left the modal closable
      // by the ✕ alone.
      onClick={(event) => {
        const target = event.target as HTMLElement
        if (target === ref.current || target.classList.contains('bb-modal__scrim')) {
          onClose()
        }
      }}
    >
      {/* The scrim is ours rather than ::backdrop, and that is the whole fix
          for the blur outstaying the modal. A native backdrop is painted for
          exactly as long as the dialog is open, and the dialog stays open
          through AnimatePresence's exit - so the panel animated away while
          the blur sat at full strength, then vanished in one frame when the
          component finally unmounted. An element inside the dialog is under
          AnimatePresence's control, so it fades on the same transition as the
          panel and the two leave together. */}
      <motion.div
        className="bb-modal__scrim"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={transition}
      />
      <motion.div
        className="bb-modal"
        style={{ maxWidth }}
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.97, y: 4 }}
        transition={transition}
      >
        <div className="bb-modal__header">
          <h2 id={titleId} className="text-title">
            {title}
          </h2>
          <button
            type="button"
            className="bb-modal__close"
            onClick={onClose}
            aria-label={strings.common.closeAriaLabel}
          >
            <IconClose />
          </button>
        </div>
        <div className="bb-modal__body">{children}</div>
      </motion.div>
    </dialog>
  )
}
