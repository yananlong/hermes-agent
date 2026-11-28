/**
 * TeX tokenizer + recursive-descent parser for the terminal math grid.
 *
 * WHY a hand parser instead of KaTeX: KaTeX is 273 KB minified and the TUI is
 * a CLI that starts on every launch, so importing it would be paid by every
 * command including `hermes --version`. It also throws on partial input, and a
 * streaming assistant response is partial by definition. This parser never
 * throws: anything it does not understand becomes an `Unknown`/`Sym` node that
 * renders as its own readable source, which is the same contract
 * `mathUnicode.ts` already promises.
 *
 * The output is an AST, not a string, so `mathGrid.ts` can lay it out. Feeding
 * that painter from KaTeX MathML instead would be a drop-in change of this
 * file: the painter consumes the `Node` union below and has no TeX knowledge.
 *
 * Deliberate limits (all render as their source rather than failing): macros
 * outside `SYMBOLS`, `\begin{env}` beyond matrices/cases/aligned, and
 * `\text{...}` with nested braces.
 */

export type Node =
  | { kind: 'row'; items: Node[] }
  | { kind: 'sym'; text: string; italic: boolean; space?: number }
  | { kind: 'frac'; num: Node; den: Node }
  | { kind: 'sqrt'; body: Node; index: Node | null }
  | { kind: 'sup'; base: Node; sup: Node }
  | { kind: 'sub'; base: Node; sub: Node }
  | { kind: 'subsup'; base: Node; sub: Node; sup: Node }
  | { kind: 'bigop'; op: string; sub: Node | null; sup: Node | null }
  | { kind: 'fence'; open: string; body: Node; close: string }
  | { kind: 'accent'; accent: string; body: Node }
  | { kind: 'over'; body: Node }
  | { kind: 'matrix'; rows: Node[][]; open: string | null; close: string | null }
  | { kind: 'unknown'; text: string }

/** Greek letters, operators and relations — the glyph side of the table. */
const SYMBOLS: Record<string, string> = {
  alpha: 'α',
  beta: 'β',
  gamma: 'γ',
  delta: 'δ',
  epsilon: 'ε',
  varepsilon: 'ε',
  zeta: 'ζ',
  eta: 'η',
  theta: 'θ',
  vartheta: 'ϑ',
  iota: 'ι',
  kappa: 'κ',
  lambda: 'λ',
  mu: 'μ',
  nu: 'ν',
  xi: 'ξ',
  pi: 'π',
  varpi: 'ϖ',
  rho: 'ρ',
  varrho: 'ϱ',
  sigma: 'σ',
  varsigma: 'ς',
  tau: 'τ',
  upsilon: 'υ',
  phi: 'φ',
  varphi: 'φ',
  chi: 'χ',
  psi: 'ψ',
  omega: 'ω',
  Gamma: 'Γ',
  Delta: 'Δ',
  Theta: 'Θ',
  Lambda: 'Λ',
  Xi: 'Ξ',
  Pi: 'Π',
  Sigma: 'Σ',
  Upsilon: 'Υ',
  Phi: 'Φ',
  Psi: 'Ψ',
  Omega: 'Ω',
  infty: '∞',
  partial: '∂',
  nabla: '∇',
  emptyset: '∅',
  varnothing: '∅',
  forall: '∀',
  exists: '∃',
  nexists: '∄',
  neg: '¬',
  lnot: '¬',
  in: '∈',
  notin: '∉',
  ni: '∋',
  subset: '⊂',
  supset: '⊃',
  subseteq: '⊆',
  supseteq: '⊇',
  cup: '∪',
  cap: '∩',
  setminus: '∖',
  le: '≤',
  leq: '≤',
  ge: '≥',
  geq: '≥',
  ne: '≠',
  neq: '≠',
  ll: '≪',
  gg: '≫',
  approx: '≈',
  equiv: '≡',
  cong: '≅',
  sim: '∼',
  simeq: '≃',
  propto: '∝',
  perp: '⊥',
  parallel: '∥',
  mid: '∣',
  nmid: '∤',
  to: '→',
  rightarrow: '→',
  leftarrow: '←',
  leftrightarrow: '↔',
  Rightarrow: '⇒',
  Leftarrow: '⇐',
  Leftrightarrow: '⇔',
  implies: '⟹',
  iff: '⟺',
  mapsto: '↦',
  uparrow: '↑',
  downarrow: '↓',
  times: '×',
  div: '÷',
  pm: '±',
  mp: '∓',
  cdot: '⋅',
  circ: '∘',
  bullet: '•',
  star: '⋆',
  ast: '∗',
  oplus: '⊕',
  otimes: '⊗',
  odot: '⊙',
  ldots: '…',
  cdots: '⋯',
  dots: '…',
  vdots: '⋮',
  ddots: '⋱',
  hbar: 'ℏ',
  hslash: 'ℏ',
  ell: 'ℓ',
  Re: 'ℜ',
  Im: 'ℑ',
  aleph: 'ℵ',
  angle: '∠',
  triangle: '△',
  langle: '⟨',
  rangle: '⟩',
  lceil: '⌈',
  rceil: '⌉',
  lfloor: '⌊',
  rfloor: '⌋',
  therefore: '∴',
  because: '∵',
  top: '⊤',
  bot: '⊥',
  '\u0024': '$',
  '%': '%',
  '&': '&',
  '#': '#',
  _: '_',
  '{': '{',
  '}': '}'
}

/** Operators that take limits above/below in display style. */
const BIGOPS: Record<string, string> = {
  sum: '∑',
  prod: '∏',
  coprod: '∐',
  int: '∫',
  iint: '∬',
  iiint: '∭',
  oint: '∮',
  bigcup: '⋃',
  bigcap: '⋂',
  bigvee: '⋁',
  bigwedge: '⋀',
  bigoplus: '⨁',
  bigotimes: '⨂',
  lim: 'lim',
  limsup: 'limsup',
  liminf: 'liminf',
  sumlimits: '∑'
}

/** Function names: upright, and never italic. */
const FUNCS = new Set([
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

/** `\mathbb` / `\mathcal` / `\mathfrak` / `\mathbf` single-letter maps. */
const BB: Record<string, string> = {
  A: '𝔸',
  B: '𝔹',
  C: 'ℂ',
  D: '𝔻',
  E: '𝔼',
  F: '𝔽',
  G: '𝔾',
  H: 'ℍ',
  I: '𝕀',
  J: '𝕁',
  K: '𝕂',
  L: '𝕃',
  M: '𝕄',
  N: 'ℕ',
  O: '𝕆',
  P: 'ℙ',
  Q: 'ℚ',
  R: 'ℝ',
  S: '𝕊',
  T: '𝕋',
  U: '𝕌',
  V: '𝕍',
  W: '𝕎',
  X: '𝕏',
  Y: '𝕐',
  Z: 'ℤ'
}

const CAL: Record<string, string> = {
  A: '𝒜',
  B: 'ℬ',
  C: '𝒞',
  D: '𝒟',
  E: 'ℰ',
  F: 'ℱ',
  G: '𝒢',
  H: 'ℋ',
  I: 'ℐ',
  J: '𝒥',
  K: '𝒦',
  L: 'ℒ',
  M: 'ℳ',
  N: '𝒩',
  O: '𝒪',
  P: '𝒫',
  Q: '𝒬',
  R: 'ℛ',
  S: '𝒮',
  T: '𝒯',
  U: '𝒰',
  V: '𝒱',
  W: '𝒲',
  X: '𝒳',
  Y: '𝒴',
  Z: '𝒵'
}

const FRAK: Record<string, string> = {
  A: '𝔄',
  B: '𝔅',
  C: 'ℭ',
  D: '𝔇',
  E: '𝔈',
  F: '𝔉',
  G: '𝔊',
  H: 'ℌ',
  I: 'ℑ',
  J: '𝔍',
  K: '𝔎',
  L: '𝔏',
  M: '𝔐',
  N: '𝔑',
  O: '𝔒',
  P: '𝔓',
  Q: '𝔔',
  R: 'ℜ',
  S: '𝔖',
  T: '𝔗',
  U: '𝔘',
  V: '𝔙',
  W: '𝔚',
  X: '𝔛',
  Y: '𝔜',
  Z: 'ℨ'
}

/** Accent commands → combining diacritic. */
const ACCENTS: Record<string, string> = {
  hat: '\u0302',
  bar: '\u0304',
  overline: '\u0305',
  tilde: '\u0303',
  vec: '\u20D7',
  dot: '\u0307',
  ddot: '\u0308',
  check: '\u030C',
  breve: '\u0306'
}

/** Spacing commands, in cells (a cell is roughly 1em/2 at terminal scale). */
const SPACES: Record<string, number> = {
  ',': 0,
  ';': 1,
  ':': 1,
  '!': 0,
  ' ': 1,
  quad: 3,
  qquad: 6,
  enspace: 1,
  thinspace: 0,
  medspace: 1,
  thickspace: 2
}

/** `\left` / `\right` delimiter spellings → the glyph we paint. */
const DELIMS: Record<string, string> = {
  '(': '(',
  ')': ')',
  '[': '[',
  ']': ']',
  '|': '|',
  '.': '',
  '\\{': '{',
  '\\}': '}',
  '\\|': '‖',
  langle: '⟨',
  rangle: '⟩',
  lceil: '⌈',
  rceil: '⌉',
  lfloor: '⌊',
  rfloor: '⌋',
  vert: '|',
  Vert: '‖'
}

type Token =
  | { t: 'cmd'; v: string }
  | { t: 'char'; v: string }
  | { t: 'group'; v: Token[] }
  | { t: 'sup' }
  | { t: 'sub' }
  | { t: 'bracket'; v: Token[] }

const isSpace = (c: string) => c === ' ' || c === '\t' || c === '\n' || c === '\r'

/**
 * Tokenize TeX into commands, characters, `{...}` groups and `_`/`^` markers.
 * Never throws: an unterminated group is returned as whatever was collected,
 * so a half-streamed `\frac{a}{` still lays out its numerator.
 */
export function tokenize(src: string): Token[] {
  let i = 0

  const readBracket = (): Token[] => {
    // at `[`; collect a balanced group terminated by `]`
    const toks: Token[] = []
    let depth = 1
    i++

    while (i < src.length) {
      const c = src[i]!

      if (c === '[') {
        depth++
      } else if (c === ']') {
        depth--

        if (depth === 0) {
          i++

          return toks
        }
      }

      if (c === '{') {
        const g = readGroup()

        toks.push({ t: 'group', v: g })

        continue
      }

      if (c === '\\') {
        toks.push(readCmd())

        continue
      }

      toks.push({ t: 'char', v: c })
      i++
    }

    return toks
  }

  const readGroup = (): Token[] => {
    const toks: Token[] = []
    let depth = 1
    i++

    while (i < src.length && depth > 0) {
      const c = src[i]!

      if (c === '}') {
        depth--

        if (depth === 0) {
          i++

          return toks
        }
      }

      if (c === '{') {
        toks.push({ t: 'group', v: readGroup() })

        continue
      }

      if (c === '\\') {
        toks.push(readCmd())

        continue
      }

      if (c === '^') {
        toks.push({ t: 'sup' })
        i++

        continue
      }

      if (c === '_') {
        toks.push({ t: 'sub' })
        i++

        continue
      }

      toks.push({ t: 'char', v: c })
      i++
    }

    return toks
  }

  const readCmd = (): Token => {
    i++ // consume backslash

    if (i >= src.length) {
      return { t: 'cmd', v: '' }
    }

    const c = src[i]!

    // Symbol commands: `\{`, `\,`, `\%`, `\\`, `\ `
    if (!/[A-Za-z]/.test(c)) {
      i++

      return { t: 'cmd', v: c }
    }

    let name = ''

    while (i < src.length && /[A-Za-z]/.test(src[i]!)) {
      name += src[i]
      i++
    }

    return { t: 'cmd', v: name }
  }

  const out: Token[] = []

  while (i < src.length) {
    const c = src[i]!

    if (isSpace(c)) {
      i++

      continue
    }

    if (c === '{') {
      out.push({ t: 'group', v: readGroup() })

      continue
    }

    if (c === '\\') {
      out.push(readCmd())

      continue
    }

    if (c === '^') {
      out.push({ t: 'sup' })
      i++

      continue
    }

    if (c === '_') {
      out.push({ t: 'sub' })
      i++

      continue
    }

    if (c === '[') {
      out.push({ t: 'bracket', v: readBracket() })

      continue
    }

    out.push({ t: 'char', v: c })
    i++
  }

  return out
}

// ---- parser --------------------------------------------------------------

/** Commands whose meaning is structural: they consume following tokens. */
const STRUCTURAL = new Map<string, (p: Parser) => Node>([
  ['frac', p => ({ kind: 'frac', num: p.argOrEmpty(), den: p.argOrEmpty() })],
  ['dfrac', p => ({ kind: 'frac', num: p.argOrEmpty(), den: p.argOrEmpty() })],
  ['tfrac', p => ({ kind: 'frac', num: p.argOrEmpty(), den: p.argOrEmpty() })],
  [
    'sqrt',
    p => {
      const index = p.peek()?.t === 'bracket' ? p.takeBracket() : null

      return { kind: 'sqrt', body: p.argOrEmpty(), index }
    }
  ],
  [
    'left',
    p => {
      const open = p.readDelim()
      const body = p.parseFenceBody(open)

      return { kind: 'fence', open, body: body.node, close: body.close }
    }
  ],
  ['overline', p => ({ kind: 'over', body: p.argOrEmpty() })],
  ['bar', p => ({ kind: 'over', body: p.argOrEmpty() })],
  ['underline', p => p.argOrEmpty()]
])

const SIZING = /^(?:big|Big|bigg|Bigg)[lrm]?$/
const STYLE = /^(?:scriptscriptstyle|displaystyle|scriptstyle|textstyle|nolimits|limits)$/

class Parser {
  private toks: Token[]
  private pos = 0

  constructor(toks: Token[]) {
    this.toks = toks
  }

  peek(offset = 0): Token | undefined {
    return this.toks[this.pos + offset]
  }

  private next(): Token | undefined {
    return this.toks[this.pos++]
  }

  /** Parse until end of input, or until `}` which the caller consumed. */
  parseRow(): Node {
    const items: Node[] = []

    while (this.pos < this.toks.length) {
      const node = this.parseScripted()

      if (node) {
        items.push(node)
      }
    }

    return items.length === 1 ? items[0]! : { kind: 'row', items }
  }

  /**
   * Parse one atom plus any `_`/`^` that follow it. A base with both scripts
   * becomes `subsup`; a big operator keeps its limits so the painter can
   * stack them above/below instead of beside.
   */
  private parseScripted(): Node | null {
    const base = this.parseAtom()

    if (!base) {
      return null
    }

    let sub: Node | null = null
    let sup: Node | null = null

    for (;;) {
      const t = this.peek()

      if (!t) {
        break
      }

      if (t.t === 'sub') {
        this.next()
        sub = this.parseScriptArg()

        continue
      }

      if (t.t === 'sup') {
        this.next()
        sup = this.parseScriptArg()

        continue
      }

      break
    }

    if (sub && sup) {
      if (base.kind === 'bigop') {
        return { kind: 'bigop', op: base.op, sub, sup }
      }

      return { kind: 'subsup', base, sub, sup }
    }

    if (sub) {
      if (base.kind === 'bigop') {
        return { kind: 'bigop', op: base.op, sub, sup: null }
      }

      return { kind: 'sub', base, sub }
    }

    if (sup) {
      if (base.kind === 'bigop') {
        return { kind: 'bigop', op: base.op, sub: null, sup }
      }

      return { kind: 'sup', base, sup }
    }

    return base
  }

  private parseScriptArg(): Node {
    const t = this.next()

    if (!t) {
      return { kind: 'row', items: [] }
    }

    if (t.t === 'group') {
      return parseTokens(t.v)
    }

    if (t.t === 'char') {
      return { kind: 'sym', text: t.v, italic: /[A-Za-z]/.test(t.v) }
    }

    if (t.t === 'cmd') {
      return this.commandNode(t.v)
    }

    return { kind: 'row', items: [] }
  }

  private parseAtom(): Node | null {
    const t = this.next()

    if (!t) {
      return null
    }

    switch (t.t) {
      case 'char':
        return { kind: 'sym', text: t.v, italic: /[A-Za-z]/.test(t.v) }

      case 'group':
        return parseTokens(t.v)

      case 'bracket':
        // A bare `[` in text position; `\sqrt[3]` handles its own degree.
        return { kind: 'sym', text: '[' + renderSource(parseTokens(t.v)) + ']', italic: false }

      case 'cmd':
        return this.commandNode(t.v)

      case 'sup':
        // `^` with no base: a stray marker. Render as its source so a
        // half-streamed expression stays legible.
        return { kind: 'sym', text: '^', italic: false }

      case 'sub':
        return { kind: 'sym', text: '_', italic: false }
    }
  }

  /** Consume the pending `[...]` token (a `\sqrt[3]` degree). */
  takeBracket(): Node {
    const b = this.next()

    return b && b.t === 'bracket' ? parseTokens(b.v) : { kind: 'row', items: [] }
  }

  argOrEmpty(): Node {
    const t = this.peek()

    if (t && t.t === 'group') {
      this.next()

      return parseTokens(t.v)
    }

    const atom = this.parseAtom()

    return atom ?? { kind: 'row', items: [] }
  }

  /**
   * Dispatch one command name to its handler.
   *
   * A table, not a branch ladder: the parser has ~40 command families and an
   * `if` chain through them was both unreadable and over the complexity cap.
   * Handlers are methods so they can consume arguments from the token stream.
   */
  private commandNode(name: string): Node {
    const table = STRUCTURAL.get(name)

    if (table) {return table(this)}

    const accent = ACCENTS[name]

    if (accent) {return { kind: 'accent', accent, body: this.argOrEmpty() }}

    if (name === 'begin') {return this.readEnvironment()}

    const styled = this.styledLetter(name)

    if (styled) {return styled}

    const bigop = BIGOPS[name]

    if (bigop) {return { kind: 'bigop', op: bigop, sub: null, sup: null }}

    if (FUNCS.has(name)) {return { kind: 'sym', text: name, italic: false }}

    if (name in SPACES) {return { kind: 'sym', text: '', italic: false, space: SPACES[name]! }}

    if (SIZING.test(name) || STYLE.test(name)) {return { kind: 'row', items: [] }}

    if (name === 'right') {
      // An unbalanced `\right` — consume its delimiter so it cannot leak.
      this.readDelim()

      return { kind: 'row', items: [] }
    }

    if (name === 'pmod' || name === 'pod' || name === 'tag') {return this.modifier(name)}

    if (DELIMS[name]) {return { kind: 'sym', text: DELIMS[name]!, italic: false }}

    if (SYMBOLS[name] !== undefined) {return { kind: 'sym', text: SYMBOLS[name]!, italic: false }}

    // Unknown: keep the source readable rather than dropping it.
    return { kind: 'unknown', text: '\\' + name }
  }

  /** `\mathbb{X}` / `\mathcal{X}` / `\mathfrak{X}`, and the upright wrappers. */
  private styledLetter(name: string): Node | null {
    if (name === 'mathbf' || name === 'mathit' || name === 'mathrm' || name === 'operatorname') {
      return this.argOrEmpty()
    }

    if (name === 'text' || name === 'textrm') {return flattenUpright(this.argOrEmpty())}

    const map = name === 'mathbb' ? BB : name === 'mathcal' ? CAL : name === 'mathfrak' ? FRAK : null

    if (!map) {return null}

    const body = this.argOrEmpty()
    const letter = body.kind === 'sym' ? body.text : ''

    return letter.length === 1 && map[letter] ? { kind: 'sym', text: map[letter]!, italic: false } : body
  }

  /** `\pmod{p}` / `\pod{p}` / `\tag{n}` — an inline parenthetical. */
  private modifier(name: string): Node {
    const body = this.argOrEmpty()
    const open = name === 'tag' ? ' (' : name === 'pod' ? ' (' : ' (mod '

    return {
      kind: 'row',
      items: [{ kind: 'sym', text: open, italic: false }, body, { kind: 'sym', text: ')', italic: false }]
    }
  }

  readDelim(): string {
    const t = this.next()

    if (!t) {
      return ''
    }

    if (t.t === 'char') {
      return DELIMS[t.v] ?? t.v
    }

    if (t.t === 'cmd') {
      return DELIMS[t.v] ?? DELIMS['\\' + t.v] ?? ''
    }

    return ''
  }

  /**
   * Collect a `\left( … \right)` body. Stops at the matching `\right` so the
   * fence can be stretched to the body's height after it is laid out.
   */
  parseFenceBody(open: string): { node: Node; close: string } {
    const items: Node[] = []
    let close = ''

    while (this.pos < this.toks.length) {
      const t = this.peek()

      if (t?.t === 'cmd' && t.v === 'right') {
        this.next()
        close = this.readDelim()

        break
      }

      const node = this.parseScripted()

      if (node) {
        items.push(node)
      }
    }

    return { node: items.length === 1 ? items[0]! : { kind: 'row', items }, close }
  }

  /** `\begin{pmatrix} a & b \\ c & d \end{pmatrix}` and friends. */
  private readEnvironment(): Node {
    const nameNode = this.argOrEmpty()
    const env = renderSource(nameNode).trim()

    const rows: Node[][] = []
    let row: Node[] = []
    const items: Node[] = []

    while (this.pos < this.toks.length) {
      const t = this.peek()

      if (t?.t === 'cmd' && t.v === 'end') {
        this.next()
        // consume the env name
        this.argOrEmpty()

        break
      }

      if (t?.t === 'char' && t.v === '&') {
        this.next()
        // A FRESH array: pushing `items` then clearing it in place would empty
        // the cell we just stored (same reference), which rendered every
        // matrix cell as the last value parsed.
        row.push({ kind: 'row', items: items.splice(0, items.length) })

        continue
      }

      if (t?.t === 'cmd' && t.v === '\\') {
        this.next()
        row.push({ kind: 'row', items: items.splice(0, items.length) })
        rows.push(row)
        row = []

        continue
      }

      const node = this.parseScripted()

      if (node) {
        items.push(node)
      }
    }

    if (items.length || row.length) {
      row.push({ kind: 'row', items: items.splice(0, items.length) })
      rows.push(row)
    }

    const clean = rows.filter(r => r.length > 0)

    const open =
      env === 'pmatrix' ? '(' : env === 'bmatrix' ? '[' : env === 'vmatrix' ? '|' : env === 'Bmatrix' ? '{' : null

    const close =
      env === 'pmatrix' ? ')' : env === 'bmatrix' ? ']' : env === 'vmatrix' ? '|' : env === 'Bmatrix' ? '}' : null

    return { kind: 'matrix', rows: clean, open, close }
  }
}

/** Render a node back to a plain string — used for `\text{}` and diagnostics. */
export function renderSource(node: Node): string {
  switch (node.kind) {
    case 'row':
      return node.items.map(renderSource).join('')

    case 'sym':
      return node.text

    case 'unknown':
      return node.text

    case 'frac':
      return `${renderSource(node.num)}/${renderSource(node.den)}`

    case 'sqrt':
      return '√' + renderSource(node.body)

    case 'sup':
      return renderSource(node.base) + '^' + renderSource(node.sup)

    case 'sub':
      return renderSource(node.base) + '_' + renderSource(node.sub)

    case 'subsup':
      return `${renderSource(node.base)}_${renderSource(node.sub)}^${renderSource(node.sup)}`

    case 'bigop':
      return node.op

    case 'fence':
      return node.open + renderSource(node.body) + node.close

    case 'accent':
      return renderSource(node.body) + node.accent

    case 'over':
      return renderSource(node.body)

    case 'matrix':
      return node.rows.map(r => r.map(renderSource).join(' ')).join('; ')
  }
}

/** Force every symbol in a subtree upright — the `\text{}` contract. */
function flattenUpright(node: Node): Node {
  switch (node.kind) {
    case 'row':
      return { kind: 'row', items: node.items.map(flattenUpright) }

    case 'sym':
      return { ...node, italic: false }

    default:
      return node
  }
}

function parseTokens(toks: Token[]): Node {
  return new Parser(toks).parseRow()
}

/** Parse a TeX string. Total: never throws, never returns null. */
export function parseTex(src: string): Node {
  return parseTokens(tokenize(src))
}
