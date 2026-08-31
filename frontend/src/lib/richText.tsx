import { Fragment, type ReactNode } from 'react'
import { splitLinks } from './linkTokens'

/**
 * Renders a strings.json value that marks inline code with backticks
 * (`` `jbg.config.jet` ``) as real <code> elements - the markdown convention
 * a non-programmer editing strings.json will already know, so code spans
 * survive editing instead of requiring hand-written JSX per string.
 */
export function renderRich(text: string): ReactNode {
  const parts = text.split(/`([^`]+)`/g)
  return parts.map((part, i) =>
    // split() with a capturing group alternates plain/code/plain/code...,
    // starting and ending on plain (odd indices are always the code spans).
    i % 2 === 1 ? <code key={i}>{part}</code> : <Fragment key={i}>{renderLinks(part)}</Fragment>,
  )
}

/** [label](https://…) inside a non-code run. Splitting is lib/linkTokens.ts's
 * job (JSX-free, so it is the one part of this file's logic that this
 * project's plain `node --test` runner can actually unit test); this is just
 * the JSX built from its tokens. A link's own label can still be **bold**. */
function renderLinks(text: string): ReactNode {
  const tokens = splitLinks(text)
  if (tokens.length === 1 && tokens[0].url === undefined) return renderBold(text)
  return tokens.map((token, i) =>
    token.url ? (
      <a key={i} href={token.url} target="_blank" rel="noopener noreferrer">
        {renderBold(token.text)}
      </a>
    ) : (
      <Fragment key={i}>{renderBold(token.text)}</Fragment>
    ),
  )
}

/** **bold** inside a non-code, non-link run. Same split trick, one level down. */
function renderBold(text: string): ReactNode {
  const parts = text.split(/\*\*([^*]+)\*\*/g)
  if (parts.length === 1) return text
  return parts.map((part, i) =>
    i % 2 === 1 ? <strong key={i}>{part}</strong> : <Fragment key={i}>{part}</Fragment>,
  )
}

/**
 * Line-separated paragraphs, with runs of "- " lines collected into a list -
 * originally written for the changelog body, also used by InfoScreen's link
 * popups (about.json's popupText, which needs its own paragraph per crypto
 * address rather than one run-on line - renderRich alone has no concept of a
 * line break, so newlines in the JSON just vanished into HTML's normal
 * whitespace collapsing). Deliberately not a markdown library - the whole
 * grammar is three rules.
 */
export function renderChangelogBody(text: string): ReactNode {
  const blocks: ReactNode[] = []
  let bullets: string[] = []

  const flushBullets = () => {
    if (bullets.length === 0) return
    blocks.push(
      <ul key={`ul-${blocks.length}`}>
        {bullets.map((item, i) => (
          <li key={i}>{renderRich(item)}</li>
        ))}
      </ul>,
    )
    bullets = []
  }

  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed.startsWith('- ')) {
      bullets.push(trimmed.slice(2))
      continue
    }
    flushBullets()
    if (trimmed) blocks.push(<p key={`p-${blocks.length}`}>{renderRich(trimmed)}</p>)
  }
  flushBullets()
  return blocks
}
