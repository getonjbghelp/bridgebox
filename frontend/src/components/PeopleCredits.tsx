import { AnimatePresence } from 'framer-motion'
import { useState, type ReactNode } from 'react'
import { Section } from './Section'
import { Modal } from './Modal'
import { IconInfo } from './icons'
import { renderRich } from '../lib/richText'
import { PEOPLE, localeText, type BugHunter, type Donator, type Other, type Tester } from '../lib/content'
import { useStrings } from '../lib/strings'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import './PeopleCredits.css'

type Category = 'donators' | 'bughunters' | 'testers' | 'other'
type Person = Donator | BugHunter | Tester | Other

/**
 * "Спасибо" - donators, bug hunters, testers, and anyone else worth naming
 * for something that doesn't fit those three. Distinct from the open-source
 * attribution ticker on HomeScreen (data/credits.ts): this is people who
 * supported THIS project, not the libraries it's built on. Data comes from
 * PEOPLE (lib/content.ts), written by tools/build_content.py.
 */
export function PeopleCredits() {
  const strings = useStrings()
  const [openPerson, setOpenPerson] = useState<{ category: Category; person: Person } | null>(null)

  // One flat list, not a tab per category: everybody here is being thanked,
  // and which bucket a name sits in is a detail of the entry, not a filter
  // the reader came to apply. The category still travels with each person so
  // the modal knows which fields to show. Keyed by category+id because the
  // same id may legitimately appear in two categories (someone who donated
  // AND tested) - see validate_people's "same id in two categories is fine".
  const everyone: { category: Category; person: Person }[] = [
    ...PEOPLE.donators.map((person) => ({ category: 'donators' as const, person: person as Person })),
    ...PEOPLE.bughunters.map((person) => ({ category: 'bughunters' as const, person: person as Person })),
    ...PEOPLE.testers.map((person) => ({ category: 'testers' as const, person: person as Person })),
    ...PEOPLE.other.map((person) => ({ category: 'other' as const, person: person as Person })),
  ]

  // Nothing to show until build_content.py has written at least one entry -
  // same rule InfoScreen already applies to the Links section.
  if (everyone.length === 0) return null

  return (
    <Section title={strings.info.peopleTitle}>
      <div className="bb-people__pills">
        {everyone.map((entry) => (
          <button
            key={`${entry.category}:${entry.person.id}`}
            type="button"
            className="bb-people__pill"
            onClick={() => setOpenPerson(entry)}
          >
            <PersonAvatar person={entry.person} />
            <span className="bb-people__pill-name">{entry.person.name}</span>
            <IconInfo size={14} />
          </button>
        ))}
      </div>

      <AnimatePresence>
        {openPerson && (
          <Modal title={openPerson.person.name} onClose={() => setOpenPerson(null)}>
            <PersonDetails category={openPerson.category} person={openPerson.person} />
          </Modal>
        )}
      </AnimatePresence>
    </Section>
  )
}

function PersonAvatar({ person }: { person: Person }) {
  if (person.avatar) {
    return <img className="bb-people__avatar" src={person.avatar} alt="" />
  }
  const initial = person.name.trim().charAt(0).toUpperCase() || '?'
  return (
    <span className="bb-people__avatar bb-people__avatar--fallback" aria-hidden="true">
      {initial}
    </span>
  )
}

function PersonDetails({ category, person }: { category: Category; person: Person }) {
  const strings = useStrings()
  const { locale } = useMotionPrefs()

  if (category === 'donators') {
    const donator = person as Donator
    return (
      <dl className="bb-people__detail-list">
        <Detail label={strings.info.peopleDonatorDate} value={donator.date} />
        <Detail label={strings.info.peopleDonatorPlatform} value={donator.platform} />
        {donator.amount && <Detail label={strings.info.peopleDonatorAmount} value={donator.amount} />}
        {donator.comment && localeText(donator.comment, locale) && (
          <Detail
            label={strings.info.peopleDonatorComment}
            value={renderRich(localeText(donator.comment, locale))}
          />
        )}
      </dl>
    )
  }

  if (category === 'bughunters') {
    const hunter = person as BugHunter
    return (
      <dl className="bb-people__detail-list">
        <Detail label={strings.info.peopleBughunterBug} value={localeText(hunter.bugTitle, locale)} />
        <Detail value={renderRich(localeText(hunter.bugDescription, locale))} />
        {hunter.link && (
          <Detail
            label={strings.info.peopleBughunterLink}
            value={
              <a href={hunter.link} target="_blank" rel="noopener noreferrer">
                {hunter.link}
              </a>
            }
          />
        )}
      </dl>
    )
  }

  if (category === 'testers') {
    const tester = person as Tester
    return (
      <dl className="bb-people__detail-list">
        <Detail label={strings.info.peopleTesterTested} value={localeText(tester.tested, locale)} />
        <Detail label={strings.info.peopleTesterEnvironment} value={tester.environment} />
        <Detail
          label={strings.info.peopleTesterContribution}
          value={renderRich(localeText(tester.contribution, locale))}
        />
      </dl>
    )
  }

  const other = person as Other
  return (
    <dl className="bb-people__detail-list">
      <Detail label={strings.info.peopleOtherReason} value={renderRich(localeText(other.reason, locale))} />
    </dl>
  )
}

function Detail({ label, value }: { label?: string; value: ReactNode }) {
  return (
    <div className="bb-people__detail-row">
      {label && <dt className="text-caption">{label}</dt>}
      <dd className="text-body">{value}</dd>
    </div>
  )
}
