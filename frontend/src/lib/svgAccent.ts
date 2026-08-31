const PAINT = /(?:fill|stroke|stop-color)\s*[:=]\s*["']?(#[0-9a-f]{3,8})\b/gi
const NEUTRAL = new Set(['#fff', '#ffffff', '#000', '#000000'])
export function svgAccent(svg?: string): string | null {
  if (!svg) return null
  for (const match of svg.matchAll(PAINT)) {
    const color = match[1]
    if (!NEUTRAL.has(color.toLowerCase())) return color
  }
  return null
}
