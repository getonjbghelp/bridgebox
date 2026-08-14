/** A plain-text run, or a [label](url) run. Exactly one of the two shapes -
 * `url` present means this token is a link. */
export interface LinkToken {
  text: string
  url?: string
}

/** Splits text on [label](https://…) into alternating plain/link tokens.
 *
 * Pure and JSX-free on purpose: richText.tsx's own JSX means this project's
 * plain `node --test` runner cannot import anything from it at all (Node has
 * no JSX transform), so the one piece of this with real branching logic -
 * the regex walk - lives here, in a file that can actually be unit tested.
 * See lib/richText.tsx's renderLinks for the JSX built from these tokens.
 *
 * http(s) only, deliberately: this text is authored (strings.json,
 * about.json, changelog.json), never user input, but a stray javascript:
 * URL slipping into a content file should still render as nothing rather
 * than something clickable. */
export function splitLinks(text: string): LinkToken[] {
  const linkRe = /\[([^[\]]+)\]\((https?:\/\/[^\s)]+)\)/g
  const tokens: LinkToken[] = []
  let lastIndex = 0
  let match: RegExpExecArray | null
  while ((match = linkRe.exec(text))) {
    if (match.index > lastIndex) {
      tokens.push({ text: text.slice(lastIndex, match.index) })
    }
    tokens.push({ text: match[1], url: match[2] })
    lastIndex = match.index + match[0].length
  }
  if (lastIndex < text.length || tokens.length === 0) {
    tokens.push({ text: text.slice(lastIndex) })
  }
  return tokens
}
