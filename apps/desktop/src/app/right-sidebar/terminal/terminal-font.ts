import { atom } from 'nanostores'

// 'Source Code Pro' is bundled (src/fonts/, @font-face in src/styles.css) so the embedded
// terminal renders identically without a system font; the rest stay installed-font fallbacks.
export const DEFAULT_TERMINAL_FONT_FAMILY =
  "'Source Code Pro', 'JetBrains Mono', 'Cascadia Code', 'SF Mono', Menlo, Consolas, monospace"
export const DEFAULT_TERMINAL_FONT_SIZE = 15
export const MIN_TERMINAL_FONT_SIZE = 8
export const MAX_TERMINAL_FONT_SIZE = 32

export const TERMINAL_FONT_SUGGESTIONS = [
  'Source Code Pro',
  'MesloLGS NF',
  'JetBrainsMono Nerd Font',
  'CaskaydiaCove Nerd Font',
  'FiraCode Nerd Font',
  'Hack Nerd Font',
  'SauceCodePro Nerd Font',
  'JetBrains Mono',
  'SF Mono',
  'Menlo',
  'Cascadia Code'
] as const

/** The profile-backed value as written in config.yaml. Empty means bundled default. */
export const $terminalFontFamily = atom('')
export const $terminalFontSize = atom(DEFAULT_TERMINAL_FONT_SIZE)

export function normalizeTerminalFontFamily(value: unknown): string {
  return typeof value === 'string' ? value.trim() : ''
}

function quoteSingleFamily(value: string): string {
  return `'${value.replace(/\\/g, '\\\\').replace(/'/g, "\\'")}'`
}

/** Accept a friendly single family name or an authored CSS font stack. */
export function resolveTerminalFontFamily(value: unknown): string {
  const configured = normalizeTerminalFontFamily(value)

  if (!configured) {
    return DEFAULT_TERMINAL_FONT_FAMILY
  }

  const preferred = configured.includes(',') || /['"]/.test(configured) ? configured : quoteSingleFamily(configured)

  return `${preferred}, ${DEFAULT_TERMINAL_FONT_FAMILY}`
}

export function setTerminalFontFamilyFromConfig(value: unknown): void {
  $terminalFontFamily.set(normalizeTerminalFontFamily(value))
}

/** Keep persisted values within xterm's readable desktop range. */
export function normalizeTerminalFontSize(value: unknown): number {
  return typeof value === 'number' &&
    Number.isInteger(value) &&
    value >= MIN_TERMINAL_FONT_SIZE &&
    value <= MAX_TERMINAL_FONT_SIZE
    ? value
    : DEFAULT_TERMINAL_FONT_SIZE
}

export function setTerminalFontSizeFromConfig(value: unknown): void {
  $terminalFontSize.set(normalizeTerminalFontSize(value))
}

type FontFaceLoader = Pick<FontFaceSet, 'load'>

function browserFontSet(): FontFaceLoader | undefined {
  return typeof document === 'undefined' ? undefined : document.fonts
}

/** Warm every face xterm uses before WebGL builds its glyph texture atlas. */
export async function warmTerminalFontFamily(
  fontFamily: string,
  fontSize = DEFAULT_TERMINAL_FONT_SIZE,
  fontSet: FontFaceLoader | undefined = browserFontSet()
): Promise<void> {
  if (!fontSet?.load) {
    return
  }

  await Promise.allSettled(
    ['400', '700', 'italic 400'].map(descriptor =>
      Promise.resolve().then(() => fontSet.load(`${descriptor} ${fontSize}px ${fontFamily}`))
    )
  )
}

/**
 * Wait for the newest requested family before mounting xterm. Config can arrive
 * after the terminal component renders; this loop prevents opening WebGL with
 * stale fallback metrics and then immediately rebuilding it.
 */
export async function prepareTerminalFontFamily(
  getLatest: () => string,
  isCurrent: () => boolean,
  warm: (fontFamily: string, fontSize: number) => Promise<void> = warmTerminalFontFamily,
  getLatestFontSize: () => number = () => DEFAULT_TERMINAL_FONT_SIZE
): Promise<string | null> {
  let candidate = getLatest()
  let candidateSize = getLatestFontSize()

  while (isCurrent()) {
    await warm(candidate, candidateSize)

    if (!isCurrent()) {
      return null
    }

    const latest = getLatest()
    const latestSize = getLatestFontSize()

    if (latest === candidate && latestSize === candidateSize) {
      return candidate
    }

    candidate = latest
    candidateSize = latestSize
  }

  return null
}

export interface TerminalFontTarget {
  options: { fontFamily?: string; fontSize?: number }
  rows: number
  refresh: (start: number, end: number) => void
}

interface ApplyTerminalFontOptions {
  clearTextureAtlas: () => void
  fit: () => void
  fontFamily: string
  fontSize: number
  isCurrent: () => boolean
  term: TerminalFontTarget
  warm?: (fontFamily: string, fontSize: number) => Promise<void>
}

/** Apply a live font change without recreating the xterm instance or its PTY. */
export async function applyTerminalFont({
  clearTextureAtlas,
  fit,
  fontFamily,
  fontSize,
  isCurrent,
  term,
  warm = warmTerminalFontFamily
}: ApplyTerminalFontOptions): Promise<boolean> {
  await warm(fontFamily, fontSize)

  if (!isCurrent()) {
    return false
  }

  term.options.fontFamily = fontFamily
  term.options.fontSize = fontSize
  fit()

  try {
    clearTextureAtlas()
  } catch {
    // WebGL context lost or uninitialized — the DOM fallback has no atlas.
  }

  if (term.rows > 0) {
    term.refresh(0, term.rows - 1)
  }

  return true
}
