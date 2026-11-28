import { describe, expect, it } from 'vitest'

import { charWidth, stringWidth } from '../lib/mathGrid.js'
import { renderMathLine, renderMathLines } from '../lib/mathRender.js'
import { parseTex } from '../lib/mathTex.js'

const rows = (tex: string) => renderMathLines(tex)

describe('stringWidth — display width, not code points', () => {
  it('counts combining marks as zero width', () => {
    // θ̂ is two code points, one cell. Counting length would misalign every
    // accented column in a grid.
    expect(stringWidth('θ')).toBe(1)
    expect(stringWidth('θ\u0302')).toBe(1)
    expect(charWidth('\u0302')).toBe(0)
  })

  it('counts East-Asian wide characters as two cells', () => {
    expect(stringWidth('中')).toBe(2)
    expect(stringWidth('ab')).toBe(2)
  })
})

describe('renderMathLines — stacked fractions', () => {
  it('paints a numerator, a rule and a denominator on separate rows', () => {
    expect(rows('\\frac{1}{2}')).toEqual(['1', '─', '2'])
  })

  it('centers a wider numerator over a narrower denominator', () => {
    const out = rows('\\frac{abc}{d}')

    expect(out).toHaveLength(3)
    expect(out[1]).toBe('───')
    expect(out[2]).toBe(' d')
  })

  it('nests without losing a row', () => {
    // 1 + 1/(1+1/(1+1/x)) — three fraction rules, each on its own row.
    const out = rows('\\frac{1}{1+\\frac{1}{1+\\frac{1}{x}}}')

    expect(out.filter(r => r.includes('─')).length).toBeGreaterThanOrEqual(3)
    expect(out.join('')).toContain('x')
  })
})

describe('renderMathLines — radicals', () => {
  it('draws the vinculum over the body and puts √ on the baseline', () => {
    // The bar covers the radicand only; over the `√` itself it draws a rule
    // through the glyph.
    expect(rows('\\sqrt{\\pi}')).toEqual([' ─', '√π'])
  })

  it('keeps the bar off the radical sign itself', () => {
    const out = rows('\\sqrt{ab}')

    expect(out[0]).toBe(' ──')
    expect(out[1]).toBe('√ab')
  })

  it('renders an n-th root index before the sign', () => {
    // `\sqrt[3]{x}` — the index rides above-left, not inline as `√[3]{x}`.
    const out = rows('\\sqrt[3]{x}')

    expect(out.join('')).toContain('³')
    expect(out.join('')).toContain('√')
    expect(out.join('')).not.toContain('[')
  })
})

describe('renderMathLines — fences', () => {
  it('stretches a paren pair over a multi-row body', () => {
    const out = rows('\\left(\\frac{1}{2}\\right)')

    expect(out[0]).toContain('⎛')
    expect(out[out.length - 1]).toContain('⎝')
    expect(out.join('')).toContain('⎜')
  })

  it('keeps ASCII parens for a one-row body', () => {
    // A stretched paren around a single row reads worse than `(`.
    expect(rows('\\left(x\\right)')).toEqual(['(x)'])
  })
})

describe('renderMathLines — matrices and cases', () => {
  it('lays out a pmatrix with aligned columns', () => {
    const out = rows('A = \\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}')

    expect(out).toHaveLength(2)
    expect(out[0]).toContain('⎛')
    expect(out[1]).toContain('⎝')
    expect(out[0]).toContain('a')
    expect(out[1]).toContain('c')
  })

  it('does not alias cells to one shared array', () => {
    // Regression: pushing the live buffer then clearing it in place emptied
    // the cell just stored, so every cell painted as the last value parsed.
    const out = renderMathLine('\\begin{pmatrix} a & b \\\\ c & d \\end{pmatrix}')

    expect(out).toContain('a')
    expect(out).toContain('b')
    expect(out).toContain('c')
    expect(out).toContain('d')
  })

  it('renders a cases block with both branches', () => {
    const out = rows('|x| = \\begin{cases} x & x \\geq 0 \\\\ -x & x < 0 \\end{cases}').join('\n')

    expect(out).toContain('≥')
    expect(out).toContain('<')
  })
})

describe('renderMathLines — large operators', () => {
  it('stacks a sum limit under the operator', () => {
    const out = rows('\\sum_{n=1}^{\\infty}')

    expect(out.length).toBeGreaterThan(1)
    expect(out[0]).toContain('∞')
    expect(out[out.length - 1]).toContain('1')
  })

  it('stacks an integral limit instead of hanging it left of the sign', () => {
    // Side scripts offset to the RIGHT of a one-row box, which put `-∞` to the
    // left of `∫` and above whatever preceded it.
    const out = rows('\\int_{-\\infty}^{\\infty} e^{-x^{2}}\\,dx')

    expect(out[0]).toContain('∞')
    expect(out.join('')).toContain('∫')
    expect(out.join('')).toContain('- ∞')
  })

  it('keeps a stacked limit from colliding with the next atom', () => {
    const out = rows('\\sum_{n=1}^{\\infty} \\frac{1}{n^{2}}')

    // `n = 1` is wider than `∑`; without a cell the following atom fuses.
    expect(out.join('')).not.toContain('1n')
  })
})

describe('renderMathLines — symbols', () => {
  it('renders Greek, hbar and blackboard capitals', () => {
    expect(renderMathLine('E = \\hbar\\omega')).toBe('E = ℏω')
    expect(renderMathLine('\\mathbb{R}')).toBe('ℝ')
    expect(renderMathLine('\\alpha \\in \\Omega')).toBe('α ∈ Ω')
  })

  it('keeps function names upright and spaced', () => {
    expect(renderMathLine('\\sin x')).toBe('sin x')
    expect(renderMathLine('\\log f')).toBe('log f')
  })

  it('spaces relations and binary operators', () => {
    expect(renderMathLine('x = -b')).toBe('x = - b')
    expect(renderMathLine('a + b')).toBe('a + b')
  })

  it('attaches accents to the base row instead of drawing a row above', () => {
    // `\hat` arrives as a combining diacritic; a separate accent row turned
    // `θ̂` into a stray `^` floating above the letter.
    const out = rows('\\hat{\\theta}')

    expect(out).toHaveLength(1)
    expect(out[0]).toBe('θ\u0302')
  })
})

describe('renderMathLines — totality', () => {
  it('never throws on partial input', () => {
    // Streaming shows half an expression on most tokens; a throw would flash
    // a broken frame.
    for (const tex of ['\\frac{', '\\frac{a}{', '\\sqrt{', '\\left(', '\\begin{pmatrix}', '{', '}', '\\']) {
      expect(() => rows(tex)).not.toThrow()
    }
  })

  it('preserves an unknown command as its source', () => {
    // Raw LaTeX reads as pending text; a placeholder reads as broken.
    expect(renderMathLine('\\foobar{x}')).toContain('\\foobar')
  })

  it('returns one empty row for empty input', () => {
    expect(rows('')).toEqual([''])
    expect(rows('   ')).toEqual([''])
  })
})

describe('parseTex — the AST the painter consumes', () => {
  it('parses a fraction into named parts', () => {
    const ast = parseTex('\\frac{a}{b}')

    expect(ast.kind).toBe('frac')
  })

  it('parses a big operator with both limits', () => {
    const ast = parseTex('\\sum_{n=1}^{\\infty}')

    expect(ast.kind).toBe('bigop')

    if (ast.kind === 'bigop') {
      expect(ast.op).toBe('∑')
      expect(ast.sub).not.toBeNull()
      expect(ast.sup).not.toBeNull()
    }
  })

  it('records an unknown command rather than dropping it', () => {
    const ast = parseTex('\\nope')

    expect(ast.kind).toBe('unknown')
  })
})
