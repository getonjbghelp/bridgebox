import { AnimatePresence, motion } from 'framer-motion'
import { useEffect, useState, type ReactNode } from 'react'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { useMicroTransition } from '../lib/motion'
import { useStrings } from '../lib/strings'
import { BrandLogo, LOGO_HEIGHT, MONOGRAM_WIDTH, WORDMARK_WIDTH } from './BrandLogo'
import { BOUNCE_TOTAL_MS } from '../lib/brandLogoBounce'
import { BugReportModal } from './BugReportModal'
import { IconChevron, IconHome, IconInfo, IconLogs, IconMegaphone, IconSettings } from './icons'
import type { Screen } from '../App'
import './Sidebar.css'

// Handed to CSS rather than hardcoded in it, so the measured glyph geometry in
// BrandLogo.tsx stays the single source for both widths.
const LOGO_VARS = {
  ['--bb-logo-height' as string]: `${LOGO_HEIGHT}px`,
  ['--bb-logo-wordmark' as string]: `${WORDMARK_WIDTH}px`,
  ['--bb-logo-monogram' as string]: `${MONOGRAM_WIDTH}px`,
} as React.CSSProperties

function navItems(
  strings: ReturnType<typeof useStrings>,
): { id: Screen; label: string; icon: (props: { size?: number }) => ReactNode }[] {
  return [
    { id: 'home', label: strings.sidebar.navHome, icon: IconHome },
    { id: 'settings', label: strings.sidebar.navSettings, icon: IconSettings },
    { id: 'logs', label: strings.sidebar.navLogs, icon: IconLogs },
    { id: 'info', label: strings.sidebar.navInfo, icon: IconInfo },
  ]
}

export function Sidebar({
  active,
  onSelect,
}: {
  active: Screen
  onSelect: (screen: Screen) => void
}) {
  const strings = useStrings()
  const NAV_ITEMS = navItems(strings)
  // Was a hardcoded spring, so the sliding pill kept animating after the user
  // turned animations off. Micro, not the default spring: the pill's travel
  // is a couple nav rows, not a whole surface, and the modal-scaled duration
  // made it look like it was lagging behind the click.
  const pillTransition = useMicroTransition()
  const { sidebarCollapsed, setSidebarCollapsed } = useMotionPrefs()
  const [bugReportOpen, setBugReportOpen] = useState(false)
  // The letter-bounce easter egg. A click toggles it - starting it while at
  // rest, or stopping it early (see BrandLogo.css's own comment on how a
  // second click "smoothly" stops mid-wave: removing this attribute is all
  // it takes, the CSS transition underneath does the rest).
  const [bouncing, setBouncing] = useState(false)
  // The collapse telescope and the bounce both want the same transform axis
  // family on the same letters, and a wordmark collapsing INTO the rail
  // while still hopping would read as broken rather than playful - so
  // collapsing wins outright the moment it starts. Gated here, during
  // render, rather than reset through an effect watching sidebarCollapsed:
  // the attribute below has to go false on the SAME commit the collapse
  // does, and a derived value costs nothing an effect-plus-extra-render
  // would only have delayed by a frame.
  const isBouncing = bouncing && !sidebarCollapsed

  // A bounce that finished playing has nothing left animating, but without
  // this the state would stay "on" forever after the first click - the next
  // click would then read as "stop" instead of starting a fresh wave.
  useEffect(() => {
    if (!bouncing) return
    const id = window.setTimeout(() => setBouncing(false), BOUNCE_TOTAL_MS)
    return () => window.clearTimeout(id)
  }, [bouncing])

  return (
    <nav
      className="bb-sidebar"
      data-collapsed={sidebarCollapsed}
      data-bouncing={isBouncing}
      aria-label={strings.sidebar.ariaLabel}
      style={LOGO_VARS}
    >
      {/* One asset for both states. The wordmark contracts into its own two
          B's rather than cross-fading to a separate mark, so nothing pops and
          the brand never appears in two sizes at once.

          Always a <button>, collapsed or not - swapping it for a <span>
          while collapsed would remount the whole SVG subtree and cut the
          collapse transition off mid-flight, since a freshly mounted node
          has no "before" state left to transition from. Collapsed, the
          click is simply a no-op (see toggleBounce) rather than the element
          disappearing. */}
      <div className="bb-sidebar__brand">
        <button
          type="button"
          className="bb-logo-slot"
          onClick={() => {
            if (sidebarCollapsed) return
            setBouncing((b) => !b)
          }}
          tabIndex={sidebarCollapsed ? -1 : 0}
          aria-hidden={sidebarCollapsed || undefined}
        >
          <BrandLogo title={strings.sidebar.brandName} />
        </button>
      </div>

      <ul className="bb-sidebar__nav">
        {NAV_ITEMS.map((item) => {
          const isActive = item.id === active
          const Icon = item.icon
          return (
            <li key={item.id}>
              <button
                type="button"
                className={`bb-sidebar__item${isActive ? ' bb-sidebar__item--active' : ''}`}
                onClick={() => onSelect(item.id)}
                aria-current={isActive ? 'page' : undefined}
                // Collapsed, the label is hidden and the icon alone has to
                // carry the meaning. title gives the native tooltip and
                // aria-label keeps the button named for screen readers -
                // both free, neither needs a tooltip component.
                title={sidebarCollapsed ? item.label : undefined}
                aria-label={sidebarCollapsed ? item.label : undefined}
              >
                {isActive && (
                  <motion.span
                    className="bb-sidebar__active-pill"
                    // Collapsed and expanded are separate identities on
                    // purpose. The pill slides by animating a transform from
                    // its previously measured box; the rail's width is a CSS
                    // transition framer never observes, so across a collapse
                    // it would animate from a box that no longer exists. Two
                    // ids means it slides between items within a state and
                    // simply snaps across the width change, which is what a
                    // width change should do anyway.
                    layoutId={`sidebar-active-pill-${sidebarCollapsed}`}
                    transition={pillTransition}
                  />
                )}
                <span className="bb-sidebar__item-icon">
                  <Icon />
                </span>
                <span className="bb-sidebar__item-label">{item.label}</span>
              </button>
            </li>
          )
        })}
      </ul>

      <div className="bb-sidebar__footer">
        <button
          type="button"
          className="bb-sidebar__item bb-sidebar__bugreport"
          onClick={() => setBugReportOpen(true)}
          // Same collapsed-only tooltip pattern as the nav items above - the
          // label right there in the row already names it when expanded.
          title={sidebarCollapsed ? strings.sidebar.bugReportTooltip : undefined}
          aria-label={sidebarCollapsed ? strings.sidebar.bugReportTooltip : undefined}
        >
          <span className="bb-sidebar__item-icon">
            <IconMegaphone />
          </span>
          <span className="bb-sidebar__item-label">{strings.sidebar.bugReportTooltip}</span>
        </button>
        <button
          type="button"
          className="bb-sidebar__collapse"
          onClick={() => setSidebarCollapsed(!sidebarCollapsed)}
          title={sidebarCollapsed ? strings.sidebar.expand : strings.sidebar.collapse}
          aria-label={sidebarCollapsed ? strings.sidebar.expand : strings.sidebar.collapse}
          aria-expanded={!sidebarCollapsed}
        >
          <IconChevron />
        </button>
      </div>

      <AnimatePresence>
        {bugReportOpen && <BugReportModal onClose={() => setBugReportOpen(false)} />}
      </AnimatePresence>
    </nav>
  )
}
