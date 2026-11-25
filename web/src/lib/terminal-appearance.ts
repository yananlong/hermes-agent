import {
  DEFAULT_TERMINAL_FONT_SIZE,
  refitWhenTerminalFontLoads,
  TERMINAL_FONT_FAMILY,
  type RemeasurableTerminal,
} from "./terminal-font-refit";

export interface TerminalAppearance {
  fontFamily: string;
  fontSize: number;
}

export const DEFAULT_TERMINAL_APPEARANCE: TerminalAppearance = {
  fontFamily: TERMINAL_FONT_FAMILY,
  fontSize: DEFAULT_TERMINAL_FONT_SIZE,
};

export function terminalAppearanceFromConfig(config: Record<string, unknown>): TerminalAppearance {
  const terminal = config.terminal;
  const options = terminal && typeof terminal === "object" && !Array.isArray(terminal)
    ? terminal as Record<string, unknown>
    : {};
  const family = typeof options.font_family === "string" ? options.font_family.trim() : "";
  const preferred = family && !family.includes(",") && !/['"]/.test(family)
    ? `'${family.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`
    : family;
  const size = options.font_size;
  return {
    fontFamily: preferred ? `${preferred}, ${TERMINAL_FONT_FAMILY}` : TERMINAL_FONT_FAMILY,
    fontSize: typeof size === "number" && Number.isInteger(size) && size >= 8 && size <= 32
      ? size
      : DEFAULT_TERMINAL_FONT_SIZE,
  };
}

/** Update an existing xterm and its cell metrics without restarting its session. */
export function applyTerminalAppearance(
  term: RemeasurableTerminal & { options: { fontFamily?: string; fontSize?: number } },
  appearance: TerminalAppearance,
  fit: () => void,
): () => void {
  if (term.options.fontFamily !== appearance.fontFamily || term.options.fontSize !== appearance.fontSize) {
    term.options.fontFamily = appearance.fontFamily;
    term.options.fontSize = appearance.fontSize;
    fit();
    term.clearTextureAtlas();
    if (term.rows > 0) term.refresh(0, term.rows - 1);
  }
  return refitWhenTerminalFontLoads(term, fit, undefined, appearance.fontFamily);
}

/**
 * Terminal appearance helpers shared by the chat composer's xterm host:
 * theme colors and the layout-width-driven font tiers. Extracted from
 * ChatPage.tsx so the page file can keep shrinking (repo file-size ratchet).
 */

// Colors for the terminal body.  Matches the dashboard's dark teal canvas
// with cream foreground — we intentionally don't pick monokai or a loud
// theme, because the TUI's skin engine already paints the content; the
// terminal chrome just needs to sit quietly inside the dashboard.
export const DEFAULT_TERMINAL_BACKGROUND = "#000000";
export const DEFAULT_TERMINAL_FOREGROUND = "#f0e6d2";

export function buildTerminalTheme(background: string, foreground: string) {
  return {
    background,
    foreground,
    cursor: foreground,
    cursorAccent: background,
    selectionBackground:
      foreground.length === 7 ? `${foreground}44` : foreground,
  };
}

/**
 * CSS width for xterm font tiers.
 *
 * Prefer the terminal host's `clientWidth` — Chrome DevTools device mode often
 * keeps `window.innerWidth` at the full desktop value while the *drawn* layout
 * is phone-sized, which made us pick desktop font sizes (~14px) and look huge.
 */
export function terminalTierWidthPx(host: HTMLElement | null): number {
  if (typeof window === "undefined") return 1280;
  const fromHost = host?.clientWidth ?? 0;
  if (fromHost > 2) return Math.round(fromHost);
  const doc = document.documentElement?.clientWidth ?? 0;
  const vv = window.visualViewport;
  const inner = window.innerWidth;
  const vvw = vv?.width ?? inner;
  const layout = Math.min(inner, vvw, doc > 0 ? doc : inner);
  return Math.max(1, Math.round(layout));
}

const FONT_SIZE_TIERS = [[300, 7], [360, 8], [420, 9], [520, 10], [720, 11], [1024, 12]] as const;

export function terminalFontSizeForWidth(layoutWidthPx: number, size = DEFAULT_TERMINAL_FONT_SIZE): number {
  const tier = FONT_SIZE_TIERS.find(([maxWidth]) => layoutWidthPx < maxWidth)?.[1] ?? DEFAULT_TERMINAL_FONT_SIZE;
  return Math.max(7, Math.round(size * tier / DEFAULT_TERMINAL_FONT_SIZE));
}

export function terminalLineHeightForWidth(layoutWidthPx: number): number {
  return layoutWidthPx < 1024 ? 1.02 : 1.15;
}
