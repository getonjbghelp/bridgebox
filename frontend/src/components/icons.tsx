// Minimal hand-rolled icon set - a handful of glyphs doesn't justify an
// icon-library dependency (YAGNI). Every icon: 20x20 viewBox, currentColor.

type IconProps = { size?: number }

export function IconHome({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <path
        d="M3 9.5 10 3l7 6.5M5 8v8.5h10V8"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function IconSettings({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      {/* Ring + teeth + hub, three primitives, no seam.
          The previous attempt was one hand-written closed outline and it had a
          visible notch where the path met itself. The one before that was a
          small hub with eight detached rays, which reads as a sun at 20px.
          Here the ring is what makes it a gear rather than a star, and the
          tooth endpoints are computed on a circle instead of eyeballed. */}
      <path
        d="M16.10 10.00L18.50 10.00M14.31 14.31L16.01 16.01M10.00 16.10L10.00 18.50M5.69 14.31L3.99 16.01M3.90 10.00L1.50 10.00M5.69 5.69L3.99 3.99M10.00 3.90L10.00 1.50M14.31 5.69L16.01 3.99"
        stroke="currentColor"
        strokeWidth="2.1"
        strokeLinecap="round"
      />
      <circle cx="10" cy="10" r="6.1" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="10" cy="10" r="2.3" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  )
}

export function IconLogs({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <rect x="3" y="3" width="14" height="14" rx="2.5" stroke="currentColor" strokeWidth="1.6" />
      <path d="M6 7.5h8M6 10h8M6 12.5h5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

export function IconCopy({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <rect x="7" y="7" width="10" height="10" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M13 7V5.5A1.5 1.5 0 0 0 11.5 4h-6A1.5 1.5 0 0 0 4 5.5v6A1.5 1.5 0 0 0 5.5 13H7" stroke="currentColor" strokeWidth="1.5" />
    </svg>
  )
}

export function IconCheck({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <path d="M4 10.5 8 14.5 16 6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function IconClose({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <path d="M5 5l10 10M15 5 5 15" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

export function IconHeart({ size = 14 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="currentColor">
      <path d="M10 17.3 3.6 11c-2-2-2-4.9-.2-6.7 1.8-1.7 4.5-1.5 6.2.3l.4.4.4-.4c1.7-1.8 4.4-2 6.2-.3 1.8 1.8 1.8 4.7-.2 6.7L10 17.3Z" />
    </svg>
  )
}

export function IconChevron({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <path
        d="M12 5l-5 5 5 5"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function IconPlus({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <path
        d="M10 4.5v11M4.5 10h11"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
    </svg>
  )
}

export function IconTrash({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <path
        d="M4 6h12M8.5 6V4.5h3V6M6 6l.7 9a1 1 0 0 0 1 .9h4.6a1 1 0 0 0 1-.9L14 6M8.6 9v4.2M11.4 9v4.2"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

// The Steam mark, traced from the official wordmark asset. Unlike every icon
// above it is a filled glyph on a 256x259 grid rather than a 20x20 stroke
// drawing - a brand mark is not ours to redraw to fit the house style, so the
// original path is kept verbatim and only the fill follows currentColor.
export function IconSteam({ size = 28 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 256 259" fill="none" aria-hidden="true">
      <path
        d="M127.779 0C60.42 0 5.24 52.412 0 119.014l68.724 28.674a35.812 35.812 0 0 1 20.426-6.366c.682 0 1.356.019 2.02.056l30.566-44.71v-.626c0-26.903 21.69-48.796 48.353-48.796 26.662 0 48.352 21.893 48.352 48.796 0 26.902-21.69 48.804-48.352 48.804-.37 0-.73-.009-1.098-.018l-43.593 31.377c.028.582.046 1.163.046 1.735 0 20.204-16.283 36.636-36.294 36.636-17.566 0-32.263-12.658-35.584-29.412L4.41 164.654c15.223 54.313 64.673 94.132 123.369 94.132 70.818 0 128.221-57.938 128.221-129.393C256 57.93 198.597 0 127.779 0zM80.352 196.332l-15.749-6.568c2.787 5.867 7.621 10.775 14.033 13.47 13.857 5.83 29.836-.803 35.612-14.799a27.555 27.555 0 0 0 .046-21.035c-2.768-6.79-7.999-12.086-14.706-14.909-6.67-2.795-13.811-2.694-20.085-.304l16.275 6.79c10.222 4.3 15.056 16.145 10.794 26.46-4.253 10.314-15.998 15.195-26.22 10.895zm121.957-100.29c0-17.925-14.457-32.52-32.217-32.52-17.769 0-32.226 14.595-32.226 32.52 0 17.926 14.457 32.512 32.226 32.512 17.76 0 32.217-14.586 32.217-32.512zm-56.37-.055c0-13.488 10.84-24.42 24.2-24.42 13.368 0 24.208 10.932 24.208 24.42 0 13.488-10.84 24.421-24.209 24.421-13.359 0-24.2-10.933-24.2-24.42z"
        fill="currentColor"
      />
    </svg>
  )
}

export function IconFolder({ size = 28 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      {/* One closed outline: left edge up, the tab across the top, then the
          body. Drawn as explicit segments rather than nested arcs - the
          previous version mixed a relative curve into an absolute run and
          came out lopsided. */}
      <path
        d="M2.75 7.25V5.75a1 1 0 0 1 1-1H7.5l1.75 2.5h7a1 1 0 0 1 1 1v6.5a1 1 0 0 1-1 1H3.75a1 1 0 0 1-1-1z"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

// ---- setup wizard -------------------------------------------------------
// Drawn at 20x20 like the rest, but rendered large (56px) as a step's hero
// mark. Stroke weight stays 1.6 so they read as the same family scaled up
// rather than a second, chunkier set.

/** Security. The shackle is its own path so the wizard can animate it
 *  dropping into the body - hence no single combined outline. */
export function IconLock({ size = 20, closed = true }: IconProps & { closed?: boolean }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <rect
        x="4"
        y="9"
        width="12"
        height="8.5"
        rx="2"
        stroke="currentColor"
        strokeWidth="1.6"
      />
      <path
        className={closed ? undefined : 'bb-lock__shackle--open'}
        d="M7 9V6.5a3 3 0 0 1 6 0V9"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinecap="round"
      />
      <path d="M10 12v2.5" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

/** Network strategy. Concentric arcs plus a sweep line the wizard rotates -
 *  the sweep is a separate path with its own class for exactly that. */
export function IconRadar({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      {/* No radial stem. A line from the centre to the rim between two
          concentric rings is a power symbol, which this app already uses for
          the one control that turns the bridge on - so the sweep is a filled
          wedge only, and a blip off-centre settles what the rings mean. */}
      <circle cx="10" cy="10" r="7.6" stroke="currentColor" strokeWidth="1.6" opacity="0.35" />
      <circle cx="10" cy="10" r="4.2" stroke="currentColor" strokeWidth="1.4" opacity="0.3" />
      <path
        className="bb-radar__sweep"
        d="M10 10 10 2.4A7.6 7.6 0 0 1 15.4 4.6Z"
        fill="currentColor"
        opacity="0.4"
      />
      <circle cx="10" cy="10" r="1.2" fill="currentColor" />
      <circle cx="13.1" cy="13.4" r="1.1" fill="currentColor" opacity="0.75" />
    </svg>
  )
}

/** Updates. A cloud with a sync arrow through it. */
export function IconCloudSync({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="-1 -1 26 26" fill="none" aria-hidden="true">
      {/* The standard cloud-download outline, the shape Lucide/Feather and
          every other icon set converge on - three attempts at drawing a
          bespoke one (sync arrows under the cloud, inside it, a clock) all
          collapsed into unreadable hooks at icon size. A 24 grid rather than
          the house 20 because that is the grid the shape is defined on;
          IconSteam is the existing precedent for an icon with its own box.
          The viewBox itself is -1..25 rather than the drawn 0..24: the cloud
          arc's top edge sits right at y=0, so its 1.7 stroke poked past the
          SVG's own default clip (same clipping - not a container issue -
          BrandLogo's viewBox hit earlier). A 1-unit margin on every side
          covers it without redrawing the path. */}
      <path
        d="M12 13v8m0 0-3.5-3.5M12 21l3.5-3.5"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M20.9 15.4A5 5 0 0 0 18 6.3h-1.3A8 8 0 1 0 3 13.6"
        stroke="currentColor"
        strokeWidth="1.7"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

/** Finish. The check from IconCheck, given a ring so it reads as a state
 *  rather than as a confirm affordance. */
export function IconCheckCircle({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <circle cx="10" cy="10" r="7.8" stroke="currentColor" strokeWidth="1.6" />
      <path
        d="M6.4 10.2 8.9 12.7 13.7 7.6"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  )
}

export function IconPlay({ size = 16 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M7 5.2v9.6l8-4.8z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  )
}

// ---- sidebar / info screen ----------------------------------------------

export function IconInfo({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none">
      <circle cx="10" cy="10" r="7.3" stroke="currentColor" strokeWidth="1.6" />
      <circle cx="10" cy="6.6" r="1" fill="currentColor" />
      <path d="M10 9.5v4.6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
    </svg>
  )
}

// ---- link buttons on the info screen -------------------------------------
// House-style glyphs, not traced brand marks (unlike IconSteam) - readable at
// a glance and close enough to the platform's own mark to be recognisable,
// but not a claim of pixel accuracy. Swap for the real brand SVGs if that
// ever matters more than keeping this file dependency-free.

export function IconMail({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <rect x="2.5" y="4.5" width="15" height="11" rx="2" stroke="currentColor" strokeWidth="1.5" />
      <path d="M3.2 5.5 10 11l6.8-5.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

export function IconTelegram({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <circle cx="10" cy="10" r="7.5" stroke="currentColor" strokeWidth="1.4" />
      <path
        d="M5.3 10.1 14 6.7c.7-.3 1.3.2 1 1l-1.5 7c-.2.8-1 1-1.6.5l-2.4-1.9-1.3 1.3c-.3.3-.6.2-.7-.2l-.4-2.2 5-4.4-6 3.7-2-.6c-.7-.2-.7-.7.2-1Z"
        fill="currentColor"
      />
    </svg>
  )
}

export function IconDiscord({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M6 5.5c1.1-.5 2.2-.7 3-.7l.3.6c1.2-.2 2.2-.2 3.4 0l.3-.6c.8 0 1.9.2 3 .7 1.6 2.5 2.1 5.1 2 7.7-1.1 1-2.5 1.7-3.9 2l-.7-1.2c.6-.2 1.2-.5 1.7-.9-.1 0-.3.1-.4.2-2.7 1.3-6.1 1.3-8.8 0-.1-.1-.3-.1-.4-.2.5.4 1.1.7 1.7.9L6.5 15c-1.4-.3-2.8-1-3.9-2-.2-3.1.6-5.7 1.9-7.5Z"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinejoin="round"
      />
      <ellipse cx="7.6" cy="10.6" rx="1.1" ry="1.3" fill="currentColor" />
      <ellipse cx="12.4" cy="10.6" rx="1.1" ry="1.3" fill="currentColor" />
    </svg>
  )
}

export function IconGithub({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        d="M10 2.5c-4.1 0-7.5 3.4-7.5 7.6 0 3.4 2.1 6.2 5.1 7.2.4.1.5-.2.5-.4v-1.5c-2.1.5-2.5-.9-2.5-.9-.3-.9-.8-1.1-.8-1.1-.7-.5.1-.5.1-.5.7.1 1.1.8 1.1.8.7 1.2 1.8.8 2.2.6.1-.5.3-.8.5-1-1.7-.2-3.4-.9-3.4-3.9 0-.9.3-1.6.8-2.1-.1-.2-.4-1 .1-2.1 0 0 .7-.2 2.2.8.6-.2 1.3-.3 2-.3s1.4.1 2 .3c1.5-1 2.2-.8 2.2-.8.4 1.1.2 1.9.1 2.1.5.6.8 1.3.8 2.1 0 3-1.8 3.7-3.4 3.9.3.2.6.7.6 1.5v2.3c0 .2.1.5.6.4 3-1 5.1-3.8 5.1-7.2 0-4.2-3.4-7.6-7.6-7.6Z"
        fill="currentColor"
      />
    </svg>
  )
}

// ---- bug report -----------------------------------------------------------

export function IconMegaphone({ size = 20 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path d="M2 7.5 13 2v14.5L2 11V7.5Z" stroke="currentColor" strokeWidth="1.4" strokeLinejoin="round" />
      <rect x="0.6" y="11" width="2.1" height="3.4" rx="0.6" stroke="currentColor" strokeWidth="1.3" />
      <path d="M15.8 6.3c1.4 1 1.4 6.4 0 7.4" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" />
    </svg>
  )
}

export function IconFormDocument({ size = 18 }: IconProps) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M6.5 2.75h7.15l4.1 4.1V20a1.25 1.25 0 0 1-1.25 1.25H6.5A1.25 1.25 0 0 1 5.25 20V4A1.25 1.25 0 0 1 6.5 2.75Z"
        stroke="currentColor"
        strokeWidth="1.6"
        strokeLinejoin="round"
      />
      <path d="M13.5 2.9v3.25c0 .55.45 1 1 1h3.1" stroke="currentColor" strokeWidth="1.6" strokeLinejoin="round" />
      <path d="M8.5 12h7M8.5 15h7M8.5 18h4.4" stroke="currentColor" strokeWidth="1.45" strokeLinecap="round" />
    </svg>
  )
}

/** Every icon a link in about.json's `links` array is allowed to reference,
 * by the string key stored there. Kept in one place so build_content.py can
 * scan this exact list (see its own docstring) instead of guessing at a
 * second copy of the same set. */
export const LINK_ICONS = {
  telegram: IconTelegram,
  discord: IconDiscord,
  github: IconGithub,
  mail: IconMail,
  heart: IconHeart,
} satisfies Record<string, (props: IconProps) => ReturnType<typeof IconHeart>>
