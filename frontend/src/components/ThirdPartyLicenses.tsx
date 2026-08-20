import { useState } from 'react'
import { AnimatePresence } from 'framer-motion'
import { Button } from './Button'
import { Modal } from './Modal'
import { Row } from './Section'
import { CREDITS } from '../data/credits'
import { useStrings } from '../lib/strings'
import './ThirdPartyLicenses.css'

/**
 * Structured third-party license list for the Info screen - name, author,
 * license type, and a link to the project, for every open-source dependency
 * and vendored binary BridgeBox ships with (see data/credits.ts, which also
 * feeds HomeScreen's "Спасибо, X" ticker; CREDITS.md and the root
 * credits.json mirror the same entries for anyone reading the repo directly
 * rather than running the app - see credits.ts's own comment on keeping the
 * three in sync).
 */
export function ThirdPartyLicenses() {
  const strings = useStrings()
  const [open, setOpen] = useState(false)

  return (
    <>
      <Row
        label={strings.info.thirdPartyLabel}
        hint={strings.info.thirdPartyHint}
        control={
          <Button variant="secondary" onClick={() => setOpen(true)}>
            {strings.info.thirdPartyButton}
          </Button>
        }
      />
      <AnimatePresence>
        {open && (
          <Modal title={strings.info.thirdPartyModalTitle} onClose={() => setOpen(false)} maxWidth={560}>
            <ul className="bb-licenses">
              {CREDITS.map((entry) => (
                <li key={entry.name} className="bb-licenses__item">
                  <div className="bb-licenses__head">
                    <a
                      href={entry.projectUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="bb-licenses__name"
                    >
                      {entry.name}
                    </a>
                    <span className="bb-licenses__badge">{entry.license}</span>
                  </div>
                  <a
                    href={entry.authorUrl}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-caption bb-licenses__author"
                  >
                    {entry.author}
                  </a>
                  <p className="text-caption bb-licenses__purpose">{entry.purpose}</p>
                </li>
              ))}
            </ul>
          </Modal>
        )}
      </AnimatePresence>
    </>
  )
}
