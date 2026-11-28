/**
 * LaTeX AST → monospace cell grid.
 *
 * The terminal cannot typeset, but it can source one line per row of glyphs.
 * This paints a character grid instead of a string, which is what makes
 * stacked fractions, radicals with a vinculum, stretchy fences, matrices and
 * stacked limits possible at all — none of which a flat Unicode fold can
 * express.
 *
 * The box model is the load-bearing idea:
 *
 *     box = { w, h, d, rows }   // rows.length === h + d, rows[0] highest
 *
 * `h` counts rows above the baseline, `d` below. Every composer aligns
 * BASELINES (row index `h`), never tops — that is why a fraction whose
 * numerator is taller than its denominator still sits on one line.
 *
 * Width is DISPLAY width, not code-point count: combining marks are 0,
 * East-Asian wide and astral emoji are 2. Counting `String.length` misaligns
 * every accented column.
 *
 * Feeding this from KaTeX MathML instead of `mathTex.ts` is a front-end swap;
 * see `fromMathml` at the bottom of the file for the seam.
 */

import type { Node } from './mathTex.js'

export interface Box {
  w: number
  h: number
  d: number
  rows: string[]
}

const WIDE = /[\u1100-\u115F\u2E80-\uA4CF\uAC00-\uD7A3\uF900-\uFAFF\uFE30-\uFE6F\uFF00-\uFF60\uFFE0-\uFFE6]/
const ASTRAL = /[\u{1F300}-\u{1FAFF}]/u
const MARK = /[\p{Mn}\p{Me}]/u

/** Display width of one character, in terminal cells. */
export const charWidth = (ch: string): number => (MARK.test(ch) ? 0 : ASTRAL.test(ch) || WIDE.test(ch) ? 2 : 1)

/** Display width of a string, in terminal cells. */
export const stringWidth = (s: string): number => {
  let n = 0

  for (const ch of s) {
    n += charWidth(ch)
  }

  return n
}

const sp = (n: number) => ' '.repeat(Math.max(0, n))
const pad = (s: string, w: number) => s + sp(w - stringWidth(s))

const box = (rows: string[], h: number): Box => ({
  w: Math.max(0, ...rows.map(stringWidth)),
  h,
  d: rows.length - h,
  rows
})

export const textBox = (s: string): Box => box([s], 1)

/** Concatenate boxes on a shared baseline, with `gap` cells between them. */
export function hcat(boxes: Box[], gap = 0): Box {
  const parts = boxes.filter(Boolean)

  if (!parts.length) {
    return box([''], 1)
  }

  const h = Math.max(...parts.map(b => b.h))
  const d = Math.max(...parts.map(b => b.d))
  const rows: string[] = []

  for (let r = 0; r < h + d; r++) {
    let line = ''

    parts.forEach((b, i) => {
      const row = r - (h - b.h)
      line += row >= 0 && row < b.rows.length ? pad(b.rows[row]!, b.w) : sp(b.w)

      if (i < parts.length - 1) {
        line += sp(gap)
      }
    })

    rows.push(line)
  }

  return box(rows, h)
}

/** Center a box inside `w` cells. Oversized children widen the result. */
export function center(b: Box, w: number): Box {
  const width = Math.max(w, b.w)
  const left = Math.max(0, Math.floor((width - b.w) / 2))

  return box(
    b.rows.map(r => sp(left) + pad(r, b.w) + sp(width - b.w - left)),
    b.h
  )
}

const RULE = '─'

/** Stacked fraction: centered numerator, rule, centered denominator. */
function fracBox(num: Box, den: Box): Box {
  const w = Math.max(num.w, den.w, 1)
  const n = center(num, w)
  const d = center(den, w)

  return box([...n.rows, RULE.repeat(w), ...d.rows], n.h + 1)
}

/** Radical: vincular above, then `√` and the radicand on the baseline. */
function sqrtBox(body: Box, index: Box | null): Box {
  const radical = textBox('√')

  const foldedIndex = index && index.rows.length === 1 ? fold(index.rows.join(''), SUP) : null

  const shown = foldedIndex !== null && index ? textBox(foldedIndex) : index
  const radicand = shown ? hcat([shown, radical, body], 0) : hcat([radical, body], 0)

  // The bar sits over the body only. Starting it at the radical sign drew a
  // rule straight through the `√` glyph; starting it after the index but
  // before the sign drew it over the sign too.
  const lead = (shown ? shown.w : 0) + radical.w

  return box([sp(lead) + RULE.repeat(body.w), ...radicand.rows], radicand.h + 1)
}

/** Sub/superscripts: fold to Unicode when every glyph maps, else raise a row. */
const SUP: Record<string, string> = {
  '0': '⁰',
  '1': '¹',
  '2': '²',
  '3': '³',
  '4': '⁴',
  '5': '⁵',
  '6': '⁶',
  '7': '⁷',
  '8': '⁸',
  '9': '⁹',
  '+': '⁺',
  '-': '⁻',
  '=': '⁼',
  '(': '⁽',
  ')': '⁾',
  a: 'ᵃ',
  b: 'ᵇ',
  c: 'ᶜ',
  d: 'ᵈ',
  e: 'ᵉ',
  f: 'ᶠ',
  g: 'ᵍ',
  h: 'ʰ',
  i: 'ⁱ',
  j: 'ʲ',
  k: 'ᵏ',
  l: 'ˡ',
  m: 'ᵐ',
  n: 'ⁿ',
  o: 'ᵒ',
  p: 'ᵖ',
  r: 'ʳ',
  s: 'ˢ',
  t: 'ᵗ',
  u: 'ᵘ',
  v: 'ᵛ',
  w: 'ʷ',
  x: 'ˣ',
  y: 'ʸ',
  z: 'ᶻ'
}

const SUB: Record<string, string> = {
  '0': '₀',
  '1': '₁',
  '2': '₂',
  '3': '₃',
  '4': '₄',
  '5': '₅',
  '6': '₆',
  '7': '₇',
  '8': '₈',
  '9': '₉',
  '+': '₊',
  '-': '₋',
  '=': '₌',
  '(': '₍',
  ')': '₎',
  a: 'ₐ',
  e: 'ₑ',
  h: 'ₕ',
  i: 'ᵢ',
  j: 'ⱼ',
  k: 'ₖ',
  l: 'ₗ',
  m: 'ₘ',
  n: 'ₙ',
  o: 'ₒ',
  p: 'ₚ',
  r: 'ᵣ',
  s: 'ₛ',
  t: 'ₜ',
  u: 'ᵤ',
  v: 'ᵥ',
  x: 'ₓ'
}

const fold = (s: string, table: Record<string, string>): string | null => {
  let out = ''

  for (const ch of s) {
    const mapped = table[ch]

    if (mapped === undefined) {
      return null
    }

    out += mapped
  }

  return out
}

/**
 * Scripts. A single-row script whose every glyph has a Unicode form folds
 * inline (`x²`); anything else occupies a real row, which is the only way to
 * write `e^{iπ}` or `x^{-(ν+1)/2}` without inventing glyphs.
 */
function scriptBox(base: Box, sup: Box | null, sub: Box | null): Box {
  if (sup && !sub) {
    const flat = sup.rows.length === 1 ? fold(sup.rows.join(''), SUP) : null

    if (flat !== null) {
      return hcat([base, textBox(flat)], 0)
    }

    const above = sup.rows.map(r => sp(base.w) + r)

    return box([...above, ...base.rows.map(r => pad(r, base.w + sup.w))], base.h + sup.h)
  }

  if (sub && !sup) {
    const flat = sub.rows.length === 1 ? fold(sub.rows.join(''), SUB) : null

    if (flat !== null) {
      return hcat([base, textBox(flat)], 0)
    }

    const below = sub.rows.map(r => sp(base.w) + r)

    return box([...base.rows.map(r => pad(r, base.w + sub.w)), ...below], base.h)
  }

  if (!sup || !sub) {
    return base
  }

  // Both: sup on the upper right, sub on the lower right, sharing a column.
  const right = [...sup.rows, ...sub.rows]
  const supRows = sup.rows.length
  const total = Math.max(base.h + supRows, right.length)
  const rows: string[] = []

  for (let r = 0; r < total; r++) {
    const baseRow = r < base.rows.length ? base.rows[r]! : null
    rows.push(pad(baseRow ?? '', base.w) + (right[r] ?? ''))
  }

  return box(rows, base.h + supRows)
}

/**
 * Stacked limits for a large operator: sup centered above, sub below.
 * Display-style limits are what make `\sum_{n=1}^{\infty}` legible; an
 * integral keeps side scripts instead (see `renderNode`).
 */
function limitsBox(op: Box, sup: Box | null, sub: Box | null): Box {
  const w = Math.max(op.w, sup?.w ?? 0, sub?.w ?? 0)
  const rows: string[] = []
  let h = op.h

  if (sup) {
    rows.push(...center(sup, w).rows)
    h += sup.rows.length
  }

  rows.push(...center(op, w).rows)

  if (sub) {
    rows.push(...center(sub, w).rows)
  }

  return box(rows, h)
}

/** Three-row stretchy forms, keyed by the ASCII delimiter. */
const STRETCH: Record<string, [string, string, string]> = {
  '(': ['⎛', '⎜', '⎝'],
  ')': ['⎞', '⎟', '⎠'],
  '[': ['⎡', '⎢', '⎣'],
  ']': ['⎤', '⎥', '⎦'],
  '{': ['⎧', '⎨', '⎩'],
  '}': ['⎫', '⎬', '⎭'],
  '|': ['⎪', '⎪', '⎪'],
  '‖': ['⎟', '⎟', '⎟']
}

/**
 * Stretch a fence pair over the body's rows. Body height is only known after
 * it is laid out, so this takes the finished box — stretching first would give
 * every fence the same row count.
 */
function fenceBox(open: string, body: Box, close: string): Box {
  const n = body.rows.length
  const o = STRETCH[open]
  const c = STRETCH[close]

  if (n <= 1 || !o || !c) {
    return hcat([textBox(open), body, textBox(close)], 0)
  }

  const rows = body.rows.map((r, i) => {
    const left = n === 1 ? o[1] : i === 0 ? o[0] : i === n - 1 ? o[2] : o[1]
    const right = n === 1 ? c[1] : i === 0 ? c[0] : i === n - 1 ? c[2] : c[1]

    return left + r + right
  })

  return box(rows, body.h)
}

/** A combining accent attaches to the baseline row of its base. */
function accentBox(accent: string, base: Box): Box {
  const rows = base.rows.map((r, i) => (i === base.h - 1 ? r + accent : r))

  return box(rows, base.h)
}

/** `\overline`: a rule row above the base. */
function overBox(base: Box): Box {
  return box([RULE.repeat(Math.max(1, base.w)), ...base.rows], base.h + 1)
}

/** Matrices: columns padded to per-column max width, rows on one line each. */
function matrixBox(rows: Box[][], open: string | null, close: string | null): Box {
  if (!rows.length) {
    return textBox('')
  }

  const cols = Math.max(...rows.map(r => r.length))
  const widths = Array.from({ length: cols }, (_, c) => Math.max(0, ...rows.map(r => r[c]?.w ?? 0)))
  const lines = rows.map(row => row.map((cell, c) => pad(cell.rows[0] ?? '', widths[c]!)).join('  '))
  const body = box(lines, Math.max(1, Math.ceil(lines.length / 2)))

  return open && close ? fenceBox(open, body, close) : body
}

/** Function names read as words: they need a space after them. */
const FUNC_NAMES = new Set([
  'sin',
  'cos',
  'tan',
  'cot',
  'sec',
  'csc',
  'arcsin',
  'arccos',
  'arctan',
  'sinh',
  'cosh',
  'tanh',
  'log',
  'ln',
  'exp',
  'det',
  'dim',
  'ker',
  'deg',
  'arg',
  'gcd',
  'max',
  'min',
  'sup',
  'inf',
  'Pr',
  'mod',
  'bmod'
])

/** Relations take a full cell either side; LaTeX's thick space. */
const QUANTIFIERS = new Set(['∀', '∃', '∄', '¬', '∴', '∵'])

const RELATIONS = new Set([
  '=',
  '<',
  '>',
  '≤',
  '≥',
  '≠',
  '≈',
  '≡',
  '≅',
  '∼',
  '≃',
  '∝',
  '→',
  '⇒',
  '⇔',
  '⟹',
  '⟺',
  '↦',
  '←',
  '∈',
  '∉',
  '⊂',
  '⊃',
  '⊆',
  '⊇',
  '∣',
  '∤',
  '≪',
  '≫',
  '±',
  '∓',
  ':'
])

/** Binary operators take a medium space either side. */
const BINOPS = new Set(['+', '-', '−', '×', '÷', '⋅', '·', '∘', '∪', '∩', '⊕', '⊗', '⊙', '∧', '∨', '±', '∓'])

const symText = (n: Node | undefined): string => (n?.kind === 'sym' ? n.text.trim() : '')

/**
 * Does the pair `(left, right)` want a separating cell?
 *
 * Resolved per pair rather than by a uniform gap: a uniform one turns `f(t)`
 * into `f ( t )`, and none at all fuses `x=-b` and `∀x∈ℝ`.
 */
function needsGap(left: Node | undefined, right: Node): boolean {
  if (!left) {
    return false
  }

  const l = symText(left)
  const r = symText(right)

  if (RELATIONS.has(l) || RELATIONS.has(r)) {
    return true
  }

  if (BINOPS.has(l) || BINOPS.has(r)) {
    return true
  }

  if (QUANTIFIERS.has(l)) {
    return true
  }

  if (l && FUNC_NAMES.has(l)) {
    return true
  }

  if (r && FUNC_NAMES.has(r)) {
    return true
  }

  // A large operator carries limits hanging off it; the next atom needs air.
  if (left.kind === 'bigop' || right.kind === 'bigop') {
    return true
  }

  // Two plain symbols that are not a known command pair: `dx` after `e^{-x²}`
  // fuses without a cell, and `2\pi` reads as `2π` only because alpha is not
  // a letter here. Keep a single-letter symbol tight to its neighbours.
  return false
}

export function renderNode(node: Node): Box {
  switch (node.kind) {
    case 'row': {
      const parts: Box[] = []

      node.items.forEach((item, i) => {
        const prev = node.items[i - 1]

        if (prev && needsGap(prev, item)) {
          parts.push(textBox(' '))
        }

        parts.push(renderNode(item))
      })

      return hcat(parts, 0)
    }

    case 'sym': {
      const body = textBox(node.text)
      const extra = node.space ?? 0

      return extra ? hcat([body, textBox(sp(extra))], 0) : body
    }

    case 'unknown':
      // Preserve the source: raw LaTeX reads as pending, a placeholder reads
      // as broken.
      return textBox(node.text)

    case 'frac':
      return fracBox(renderNode(node.num), renderNode(node.den))
    case 'sqrt': {
      const index = node.index ? renderNode(node.index) : null

      return sqrtBox(renderNode(node.body), index)
    }

    case 'sup':
      return scriptBox(renderNode(node.base), renderNode(node.sup), null)

    case 'sub':
      return scriptBox(renderNode(node.base), null, renderNode(node.sub))

    case 'subsup':
      return scriptBox(renderNode(node.base), renderNode(node.sup), renderNode(node.sub))
    case 'bigop': {
      const op = textBox(node.op)
      const sup = node.sup ? renderNode(node.sup) : null
      const sub = node.sub ? renderNode(node.sub) : null

      // Every large operator stacks its limits in display style. Side scripts
      // put an integral's upper bound over whatever preceded it (`-∞` landing
      // left of `∫`) because scriptBox offsets to the RIGHT of a one-row box.
      return limitsBox(op, sup, sub)
    }

    case 'fence':
      return fenceBox(node.open, renderNode(node.body), node.close)

    case 'accent':
      return accentBox(node.accent, renderNode(node.body))

    case 'over':
      return overBox(renderNode(node.body))

    case 'matrix':
      return matrixBox(
        node.rows.map(r => r.map(renderNode)),
        node.open,
        node.close
      )
  }
}

/** Does the pair `(left, right)` want a separating cell? */
