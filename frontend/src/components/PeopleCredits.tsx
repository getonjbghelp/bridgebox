import { AnimatePresence } from 'framer-motion'
import { useState, type ReactNode } from 'react'
import { Section } from './Section'
import { Segmented, type SegmentedOption } from './Segmented'
import { Modal } from './Modal'
import { IconInfo } from './icons'
import { renderRich } from '../lib/richText'
import { PEOPLE, localeText, type BugHunter, type Donator, type Tester } from '../lib/content'
import { useStrings } from '../lib/strings'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import './PeopleCredits.css'

type Category = 'donators' | 'bughunters' | 'testers'
type Person = Donator | BugHunter | Tester

/**
 * "Спасибо" - donators, bug hunters, testers. Distinct from the open-source
 * attribution ticker on HomeScreen (data/credits.ts): this is people who
 * supported THIS project, not the libraries it's built on. Data comes from
 * PEOPLE (lib/content.ts), written by tools/build_content.py.
 */
export function PeopleCredits() {
  const strings = useStrings()
  const [openPerson, setOpenPerson] = useState<{ category: Category; person: Person } | null>(null)

  const tabs: { value: Category; label: string; list: Person[] }[] = [
    { value: 'donators' as const, label: strings.info.peopleTabDonators, list: PEOPLE.donators as Person[] },
    {
      value: 'bughunters' as const,
      label: strings.info.peopleTabBughunters,
      list: PEOPLE.bughunters as Person[],
    },
    { value: 'testers' as const, label: strings.info.peopleTabTesters, list: PEOPLE.testers as Person[] },
  ].filter((tab) => tab.list.length > 0)

  const [category, setCategory] = useState<Category | null>(null)
  const active = tabs.find((tab) => tab.value === category) ?? tabs[0]

  // Nothing to show until build_content.py has written at least one entry -
  // same rule InfoScreen already applies to the Links section.
  if (!active) return null

  const options: SegmentedOption<Category>[] = tabs.map((tab) => ({ value: tab.value, label: tab.label }))

  return (
    <Section title={strings.info.peopleTitle}>
      {tabs.length > 1 && (
        <Segmented
          value={active.value}
          options={options}
          onChange={setCategory}
          ariaLabel={strings.info.peopleTitle}
        />
      )}
      <div className="bb-people__pills">
        {active.list.map((person) => (
          <button
            key={person.id}
            type="button"
            className="bb-people__pill"
            onClick={() => setOpenPerson({ category: active.value, person })}
          >
            <PersonAvatar person={person} />
            <span className="bb-people__pill-name">{person.name}</span>
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

function Detail({ label, value }: { label?: string; value: ReactNode }) {
  return (
    <div className="bb-people__detail-row">
      {label && <dt className="text-caption">{label}</dt>}
      <dd className="text-body">{value}</dd>
    </div>
  )
}
