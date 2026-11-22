import { describe, expect, it, vi } from 'vitest'

import {
  applyTerminalFont,
  DEFAULT_TERMINAL_FONT_FAMILY,
  DEFAULT_TERMINAL_FONT_SIZE,
  MAX_TERMINAL_FONT_SIZE,
  MIN_TERMINAL_FONT_SIZE,
  normalizeTerminalFontSize,
  prepareTerminalFontFamily,
  resolveTerminalFontFamily,
  warmTerminalFontFamily
} from './terminal-font'

describe('terminal font resolution', () => {
  it('keeps the bundled stack when no preference is configured', () => {
    expect(resolveTerminalFontFamily('')).toBe(DEFAULT_TERMINAL_FONT_FAMILY)
    expect(resolveTerminalFontFamily(undefined)).toBe(DEFAULT_TERMINAL_FONT_FAMILY)
  })

  it('quotes a friendly family name and appends the bundled fallback stack', () => {
    expect(resolveTerminalFontFamily('  MesloLGS NF  ')).toBe(`'MesloLGS NF', ${DEFAULT_TERMINAL_FONT_FAMILY}`)
  })

  it('preserves an authored CSS stack before the bundled fallbacks', () => {
    expect(resolveTerminalFontFamily("'Hack Nerd Font', monospace")).toBe(
      `'Hack Nerd Font', monospace, ${DEFAULT_TERMINAL_FONT_FAMILY}`
    )
  })

  it('accepts only integer sizes in the supported range', () => {
    expect(normalizeTerminalFontSize(MIN_TERMINAL_FONT_SIZE)).toBe(MIN_TERMINAL_FONT_SIZE)
    expect(normalizeTerminalFontSize(MAX_TERMINAL_FONT_SIZE)).toBe(MAX_TERMINAL_FONT_SIZE)
    expect(normalizeTerminalFontSize(14)).toBe(14)
    expect(normalizeTerminalFontSize(7)).toBe(DEFAULT_TERMINAL_FONT_SIZE)
    expect(normalizeTerminalFontSize(33)).toBe(DEFAULT_TERMINAL_FONT_SIZE)
    expect(normalizeTerminalFontSize(14.5)).toBe(DEFAULT_TERMINAL_FONT_SIZE)
    expect(normalizeTerminalFontSize('14')).toBe(DEFAULT_TERMINAL_FONT_SIZE)
  })
})

describe('terminal font lifecycle', () => {
  it('warms regular, bold, and italic faces at the selected size', async () => {
    const load = vi.fn(async (_font: string) => [])

    await warmTerminalFontFamily('MesloLGS NF', 16, { load } as unknown as FontFaceSet)

    expect(load.mock.calls.map(([descriptor]) => descriptor)).toEqual([
      '400 16px MesloLGS NF',
      '700 16px MesloLGS NF',
      'italic 400 16px MesloLGS NF'
    ])
  })

  it('restarts initial warming when the profile family or size arrives late', async () => {
    let latestFamily = 'fallback'
    let latestSize = DEFAULT_TERMINAL_FONT_SIZE

    const warm = vi.fn(async (fontFamily: string, fontSize: number) => {
      if (fontFamily === 'fallback') {
        latestFamily = 'MesloLGS NF'
        latestSize = 15
      }
    })

    await expect(
      prepareTerminalFontFamily(
        () => latestFamily,
        () => true,
        warm,
        () => latestSize
      )
    ).resolves.toBe('MesloLGS NF')
    expect(warm.mock.calls).toEqual([
      ['fallback', DEFAULT_TERMINAL_FONT_SIZE],
      ['MesloLGS NF', 15]
    ])
  })

  it('cancels a stale initial font request before xterm mounts', async () => {
    let current = true

    const warm = vi.fn(async () => {
      current = false
    })

    await expect(
      prepareTerminalFontFamily(
        () => 'MesloLGS NF',
        () => current,
        warm
      )
    ).resolves.toBeNull()
  })

  it('does not apply a stale live font request', async () => {
    const term = {
      options: { fontFamily: 'newer', fontSize: 18 },
      rows: 24,
      refresh: vi.fn()
    }

    await expect(
      applyTerminalFont({
        clearTextureAtlas: vi.fn(),
        fit: vi.fn(),
        fontFamily: 'stale',
        fontSize: 13,
        isCurrent: () => false,
        term,
        warm: vi.fn().mockResolvedValue(undefined)
      })
    ).resolves.toBe(false)

    expect(term.options).toEqual({ fontFamily: 'newer', fontSize: 18 })
    expect(term.refresh).not.toHaveBeenCalled()
  })

  it('applies family and size in place, then refits and refreshes the buffer', async () => {
    const term = {
      options: { fontFamily: 'fallback', fontSize: DEFAULT_TERMINAL_FONT_SIZE },
      rows: 24,
      refresh: vi.fn()
    }

    const clearTextureAtlas = vi.fn()
    const fit = vi.fn()
    const warm = vi.fn().mockResolvedValue(undefined)

    await expect(
      applyTerminalFont({
        clearTextureAtlas,
        fit,
        fontFamily: "'MesloLGS NF', monospace",
        fontSize: 17,
        isCurrent: () => true,
        term,
        warm
      })
    ).resolves.toBe(true)

    expect(warm).toHaveBeenCalledWith("'MesloLGS NF', monospace", 17)
    expect(term.options).toEqual({ fontFamily: "'MesloLGS NF', monospace", fontSize: 17 })
    expect(fit).toHaveBeenCalledOnce()
    expect(clearTextureAtlas).toHaveBeenCalledOnce()
    expect(term.refresh).toHaveBeenCalledWith(0, 23)
  })
})
