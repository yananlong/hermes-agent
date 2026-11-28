/**
 * Public entry point for grid-rendered math: TeX in, rows of text out.
 *
 * `renderMathLines` is the whole surface the markdown renderer needs. It is
 * total — bad TeX never throws, it degrades to its own source — because a
 * streaming assistant response is partial by definition and a parse error
 * would flash a broken frame on every token.
 */

import { renderNode } from './mathGrid.js'
import { parseTex } from './mathTex.js'

/**
 * Render TeX to terminal rows. `display` is the `$$...$$` form; inline math
 * uses the same painter (the caller decides how to place the rows) because a
 * fraction needs the row legibility either way.
 */
export function renderMathLines(tex: string): string[] {
  const trimmed = tex.trim()

  if (!trimmed) {
    return ['']
  }

  const box = renderNode(parseTex(trimmed))

  return box.rows.map(r => r.trimEnd())
}

/** Single-line rendering for contexts that cannot grow (spinners, labels). */
export function renderMathLine(tex: string): string {
  return renderMathLines(tex).join(' ')
}
