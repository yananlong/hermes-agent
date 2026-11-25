// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $terminalFontSize, DEFAULT_TERMINAL_FONT_SIZE } from '../right-sidebar/terminal/terminal-font'

import { TerminalFontSizeSetting } from './terminal-font-size-setting'

const mocks = vi.hoisted(() => ({
  cache: vi.fn(),
  configUpdatedAt: 1,
  loadedConfig: {} as Record<string, unknown>,
  notifyError: vi.fn(),
  profileSwitch: null as null | (() => void),
  save: vi.fn()
}))

vi.mock('@/hermes', () => ({
  saveHermesConfig: (config: Record<string, unknown>, scope?: unknown) => mocks.save(config, scope)
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      settings: {
        appearance: {
          terminalFontSizeDesc: 'Set terminal text from 8 to 32 pixels.',
          terminalFontSizeReset: 'Use default size',
          terminalFontSizeTitle: 'Terminal Font Size'
        },
        config: { autosaveFailed: 'Autosave failed' }
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({
  notifyError: (...args: unknown[]) => mocks.notifyError(...args)
}))

vi.mock('../hooks/use-config-record', () => ({
  setHermesConfigCache: (config: Record<string, unknown>) => mocks.cache(config),
  useHermesConfigRecord: () => ({
    data: mocks.loadedConfig,
    dataUpdatedAt: mocks.configUpdatedAt,
    writeScope: { profile: 'work' }
  })
}))

vi.mock('../hooks/use-on-profile-switch', () => ({
  useOnProfileSwitch: (callback: () => void) => {
    mocks.profileSwitch = callback
  }
}))

async function flushAutosave() {
  await act(async () => {
    vi.advanceTimersByTime(550)
    await Promise.resolve()
    await Promise.resolve()
  })
}

describe('TerminalFontSizeSetting', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    mocks.configUpdatedAt = 1
    mocks.loadedConfig = {
      display: { skin: 'hermes' },
      terminal: { backend: 'local', cwd: '/workspace', font_size: DEFAULT_TERMINAL_FONT_SIZE }
    }
    mocks.save.mockResolvedValue({ ok: true })
    mocks.profileSwitch = null
    $terminalFontSize.set(DEFAULT_TERMINAL_FONT_SIZE)
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
    vi.useRealTimers()
  })

  it('seeds the profile value and autosaves a sparse terminal.font_size patch', async () => {
    mocks.loadedConfig = {
      display: { skin: 'hermes' },
      terminal: { backend: 'local', cwd: '/workspace', font_size: 14 }
    }
    render(<TerminalFontSizeSetting />)
    const slider = screen.getByRole('slider', { name: 'Terminal Font Size' })

    expect((slider as HTMLInputElement).value).toBe('14')
    expect(slider.getAttribute('aria-valuetext')).toBe('14 px')
    expect($terminalFontSize.get()).toBe(14)

    fireEvent.change(slider, { target: { value: '20' } })
    expect($terminalFontSize.get()).toBe(20)

    await flushAutosave()

    expect(mocks.save).toHaveBeenCalledWith({ terminal: { font_size: 20 } }, { profile: 'work' })
    const cacheUpdate = mocks.cache.mock.calls[0][0] as (previous?: Record<string, unknown>) => Record<string, unknown>
    expect(
      cacheUpdate({
        display: { skin: 'changed' },
        terminal: { backend: 'local', cwd: '/workspace', font_family: 'MesloLGS NF', font_size: 14 }
      })
    ).toEqual({
      display: { skin: 'changed' },
      terminal: { backend: 'local', cwd: '/workspace', font_family: 'MesloLGS NF', font_size: 20 }
    })
  })

  it('uses the default when config has an invalid size and exposes validated bounds', () => {
    mocks.loadedConfig = { terminal: { font_size: 40 } }
    render(<TerminalFontSizeSetting />)
    const slider = screen.getByRole('slider', { name: 'Terminal Font Size' }) as HTMLInputElement

    expect(slider.value).toBe(String(DEFAULT_TERMINAL_FONT_SIZE))
    expect(slider.min).toBe('8')
    expect(slider.max).toBe('32')
    expect($terminalFontSize.get()).toBe(DEFAULT_TERMINAL_FONT_SIZE)
  })

  it('resets the profile size to the default', async () => {
    mocks.loadedConfig = { terminal: { font_size: 18 } }
    render(<TerminalFontSizeSetting />)

    fireEvent.click(screen.getByRole('button', { name: 'Use default size' }))
    expect($terminalFontSize.get()).toBe(DEFAULT_TERMINAL_FONT_SIZE)

    await flushAutosave()

    expect(mocks.save).toHaveBeenCalledWith(
      { terminal: { font_size: DEFAULT_TERMINAL_FONT_SIZE } },
      { profile: 'work' }
    )
  })

  it('rolls back the optimistic value when autosave fails', async () => {
    mocks.loadedConfig = { terminal: { font_size: 16 } }
    mocks.save.mockRejectedValue(new Error('disk full'))
    render(<TerminalFontSizeSetting />)
    const slider = screen.getByRole('slider', { name: 'Terminal Font Size' })

    fireEvent.change(slider, { target: { value: '24' } })
    expect($terminalFontSize.get()).toBe(24)
    await flushAutosave()

    expect((slider as HTMLInputElement).value).toBe('16')
    expect($terminalFontSize.get()).toBe(16)
    expect(mocks.notifyError).toHaveBeenCalledWith(expect.any(Error), 'Autosave failed')
  })

  it('clears the previous profile size and reseeds after the new config refetches', () => {
    const sharedConfig = { terminal: { font_size: 15 } }
    mocks.loadedConfig = sharedConfig
    const view = render(<TerminalFontSizeSetting />)

    expect($terminalFontSize.get()).toBe(15)
    act(() => mocks.profileSwitch?.())
    expect($terminalFontSize.get()).toBe(DEFAULT_TERMINAL_FONT_SIZE)
    expect((screen.getByRole('slider', { name: 'Terminal Font Size' }) as HTMLInputElement).disabled).toBe(true)

    mocks.configUpdatedAt = 2
    view.rerender(<TerminalFontSizeSetting />)

    expect((screen.getByRole('slider', { name: 'Terminal Font Size' }) as HTMLInputElement).value).toBe('15')
    expect($terminalFontSize.get()).toBe(15)
  })
})
