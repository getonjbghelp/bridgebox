import { useState } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { Button } from './Button'
import { SteamAutoConfig } from './SteamAutoConfig'
import { OtherAutoConfig } from './OtherAutoConfig'
import { IconChevron, IconCopy, IconFolder, IconPlay, IconSteam } from './icons'
import { useSpringTransition } from '../lib/motion'
import { useStrings } from '../lib/strings'
import { renderRich } from '../lib/richText'
import './ConnectGuide.css'

type Platform = 'steam' | 'other'

/**
 * "Как подключить игру", split by platform.
 *
 * The two routes had been one numbered list that silently assumed Steam, so a
 * player with a standalone copy followed four steps that did not exist for
 * them. Picking a platform first is what makes each list correct rather than
 * mostly correct.
 *
 * The GIFs are deliberately not rendered until asked for: they live in
 * public/instructions and average ~6 MB each, so mounting all six would pull
 * ~34 MB the moment the modal opens, for pictures most people never expand.
 */
export function ConnectGuide({ launchOption, address }: { launchOption: string; address: string }) {
  const strings = useStrings()
  const [platform, setPlatform] = useState<Platform | null>(null)
  const transition = useSpringTransition()

  return (
    <div className="bb-guide">
      <AnimatePresence mode="wait" initial={false}>
        {platform === null ? (
          <motion.div
            key="pick"
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={transition}
          >
            <p className="bb-guide__prompt">{strings.home.instructionsPlatformPrompt}</p>
            <div className="bb-guide__tiles">
              <button
                type="button"
                className="bb-guide__tile"
                onClick={() => setPlatform('steam')}
              >
                <IconSteam />
                <span>{strings.home.instructionsPlatformSteam}</span>
              </button>
              <button
                type="button"
                className="bb-guide__tile"
                onClick={() => setPlatform('other')}
              >
                <IconFolder />
                <span>{strings.home.instructionsPlatformOther}</span>
              </button>
            </div>
          </motion.div>
        ) : (
          <motion.div
            key={platform}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={transition}
          >
            <button
              type="button"
              className="bb-guide__back"
              onClick={() => setPlatform(null)}
            >
              <span className="bb-guide__back-arrow">
                <IconChevron size={16} />
              </span>
              {strings.home.instructionsBack}
            </button>

            {platform === 'steam' ? (
              <SteamGuide launchOption={launchOption} />
            ) : (
              <OtherGuide launchOption={launchOption} address={address} />
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

function SteamGuide({ launchOption }: { launchOption: string }) {
  const strings = useStrings()
  return (
    <>
      <SteamAutoConfig />
      <p className="bb-guide__intro">{strings.home.steamIntro}</p>
      <ol className="bb-guide__steps">
        <li>{strings.home.steamStep1}</li>
        <li>
          {strings.home.steamStep2}
          <GifToggle name="gif_steam_step2" />
        </li>
        <li>
          {strings.home.steamStep3}
          <CodeRow value={launchOption} />
          <GifToggle name="gif_steam_step3" />
        </li>
        <li>
          {strings.home.steamStep4}
          <GifToggle name="gif_steam_stepfinal" />
        </li>
        <li>{strings.home.steamStep5}</li>
      </ol>
    </>
  )
}

function OtherGuide({ launchOption, address }: { launchOption: string; address: string }) {
  const strings = useStrings()
  return (
    <>
      <OtherAutoConfig />
      <p className="bb-guide__intro">{strings.home.otherIntro}</p>
      <p className="bb-guide__intro">{strings.home.otherLead}</p>

      <h3 className="bb-guide__way">{strings.home.otherWay1Title}</h3>
      <ol className="bb-guide__steps">
        <li>{strings.home.otherWay1Step1}</li>
        <li>
          {strings.home.otherWay1Step2}
          <GifToggle name="gif_other_step2" />
        </li>
        <li>
          {strings.home.otherWay1Step3}
          <CodeRow value={launchOption} />
          <GifToggle name="gif_other_step3" />
        </li>
        <li>
          {strings.home.otherWay1Step4}
          <GifToggle name="gif_other_finalstep" />
        </li>
        <li>{strings.home.otherWay1Step5}</li>
      </ol>

      <h3 className="bb-guide__way">{strings.home.otherWay2Title}</h3>
      <p className="bb-guide__intro">{strings.home.otherWay2Intro}</p>
      <ol className="bb-guide__steps">
        <li>{strings.home.otherWay2Step1}</li>
        <li>{renderRich(strings.home.otherWay2Step2)}</li>
        <li>
          {renderRich(strings.home.otherWay2Step3)}
          <CodeRow value={address} />
        </li>
        <li>{strings.home.otherWay2Step4}</li>
      </ol>
      <p className="bb-guide__intro">{strings.home.otherWay2Outro}</p>
    </>
  )
}

function CodeRow({ value }: { value: string }) {
  const strings = useStrings()
  const [copied, setCopied] = useState(false)
  return (
    <div className="bb-guide__code-row">
      <code className="text-mono bb-guide__code">{value}</code>
      <Button
        variant="ghost"
        ariaLabel={strings.home.copyAddressAriaLabel}
        onClick={() => {
          navigator.clipboard?.writeText(value)
          setCopied(true)
          window.setTimeout(() => setCopied(false), 1500)
        }}
      >
        <IconCopy />
      </Button>
      <span className="bb-guide__copied" aria-live="polite">
        {copied ? '✓' : ''}
      </span>
    </div>
  )
}

/** A step's animation, fetched only once the reader asks for it. */
function GifToggle({ name }: { name: string }) {
  const strings = useStrings()
  const [open, setOpen] = useState(false)
  const transition = useSpringTransition()

  return (
    <div className="bb-guide__gif">
      <button type="button" className="bb-guide__gif-button" onClick={() => setOpen(!open)}>
        <span className={`bb-guide__gif-icon${open ? ' bb-guide__gif-icon--open' : ''}`}>
          <IconPlay size={14} />
        </span>
        {open ? strings.home.instructionsHideGif : strings.home.instructionsShowGif}
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            className="bb-guide__gif-frame"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            transition={transition}
          >
            <img src={`instructions/${name}.gif`} alt="" loading="lazy" />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
