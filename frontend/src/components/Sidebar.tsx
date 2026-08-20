import { AnimatePresence, motion } from 'framer-motion'
import { useState, type ReactNode } from 'react'
import { useMotionPrefs } from '../state/MotionPrefsContext'
import { useSpringTransition } from '../lib/motion'
import { useStrings } from '../lib/strings'
import { BrandLogo, LOGO_HEIGHT, MONOGRAM_WIDTH, WORDMARK_WIDTH } from './BrandLogo'
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
  // turned animations off.
  const pillTransition = useSpringTransition()
  const { sidebarCollapsed, setSidebarCollapsed } = useMotionPrefs()
  const [bugReportOpen, setBugReportOpen] = useState(false)

  return (
    <nav
      className="bb-sidebar"
      data-collapsed={sidebarCollapsed}
      aria-label={strings.sidebar.ariaLabel}
      style={LOGO_VARS}
    >
      {/* One asset for both states. The wordmark contracts into its own two
          B's rather than cross-fading to a separate mark, so nothing pops and
          the brand never appears in two sizes at once. */}
      <div className="bb-sidebar__brand">
        <span className="bb-logo-slot">
          <BrandLogo title={strings.sidebar.brandName} />
        </span>
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
