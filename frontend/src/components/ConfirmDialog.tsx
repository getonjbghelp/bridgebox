import { useState } from 'react'
import { Modal } from './Modal'
import { Button } from './Button'
import { Toggle } from './Toggle'
import { useStrings } from '../lib/strings'
import './ConfirmDialog.css'

interface ConfirmDialogProps {
  title: string
  body: string
  confirmLabel: string
  danger?: boolean
  // Factory reset (and any other confirmation that must never become
  // silence-able) sets this to hide the toggle entirely - see
  // ConfirmRequest.hideSkip.
  hideSkip?: boolean
  onConfirm: (skipNextTime: boolean) => void
  onCancel: () => void
}

/**
 * A Modal specialised for "are you sure" - every reset button in Settings
 * goes through this instead of firing immediately. The "don't ask again"
 * state lives here, not in the caller: each button passes its own id to
 * confirmThenRun() (see SettingsScreen), so opting out of one confirmation
 * never silences another.
 */
export function ConfirmDialog({
  title,
  body,
  confirmLabel,
  danger,
  hideSkip,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  const strings = useStrings()
  const [skip, setSkip] = useState(false)

  return (
    <Modal title={title} onClose={onCancel}>
      <p className="text-body">{body}</p>
      {!hideSkip && (
        <div className="bb-confirm__skip">
          <Toggle checked={skip} onChange={setSkip} label={strings.common.dontAskAgain} />
          {/* Only the text is clickable, not the row: the Toggle already
              handles its own click, and a shared handler on the wrapper would
              double-fire (bubble + its own onChange) and cancel itself out. */}
          <span className="text-caption" onClick={() => setSkip((v) => !v)}>
            {strings.common.dontAskAgain}
          </span>
        </div>
      )}
      <div className="bb-confirm__actions">
        <Button variant="ghost" onClick={onCancel}>
          {strings.common.cancel}
        </Button>
        <Button variant={danger ? 'danger' : 'primary'} onClick={() => onConfirm(skip)}>
          {confirmLabel}
        </Button>
      </div>
    </Modal>
  )
}
