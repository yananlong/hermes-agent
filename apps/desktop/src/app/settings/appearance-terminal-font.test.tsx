// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $terminalFontSize, DEFAULT_TERMINAL_FONT_SIZE, setTerminalFontSizeFromConfig } from '../right-sidebar/terminal/terminal-font'

import { AppearanceSettings } from './appearance-settings'
import { SETTING_IDS, settingElementId } from './settings-manifest'

const mocks = vi.hoisted(() => ({ loadedConfig: {} as Record<string, unknown> }))

vi.mock('@/hermes', () => ({
  getHermesConfig: vi.fn(async () => mocks.loadedConfig),
  getHermesConfigDefaults: vi.fn(async () => ({})),
  saveHermesConfig: vi.fn(async () => ({ ok: true })),
  setApiRequestProfile: vi.fn()
}))

vi.mock('../hooks/use-config-record', () => ({
  setHermesConfigCache: vi.fn(),
  useHermesConfigRecord: () => ({ data: mocks.loadedConfig, dataUpdatedAt: 1, writeScope: { profile: 'work' } })
}))

function renderTypography() {
  return render(
    <QueryClientProvider client={new QueryClient()}>
      <AppearanceSettings subpage="typography" />
    </QueryClientProvider>
  )
}

describe('Appearance typography terminal controls', () => {
  beforeEach(() => {
    mocks.loadedConfig = { terminal: { font_family: '', font_size: 15 } }
    setTerminalFontSizeFromConfig(DEFAULT_TERMINAL_FONT_SIZE)
  })

  afterEach(() => {
    cleanup()
  })

  // The size control shipped with no mount point: the component and its
  // terminalFontSizeTitle copy existed, but Appearance rendered only the family
  // row, so the setting was unreachable from the UI.
  it('mounts the font size row beside the font family row', async () => {
    const { container } = renderTypography()

    expect(await screen.findByLabelText('Terminal Font')).toBeTruthy()
    expect(await screen.findByLabelText('Terminal Font Size')).toBeTruthy()

    // Both rows carry the deep-link ids the settings palette targets.
    const ids = [SETTING_IDS.appearance.terminalFont, SETTING_IDS.appearance.terminalFontSize]

    // Attribute selector: the ids contain a dot, which a `#id` selector would
    // read as a class boundary.
    for (const id of ids) {
      expect(container.querySelector(`[id="${settingElementId(id)}"]`)).toBeTruthy()
    }
  })

  it('seeds the slider from terminal.font_size and publishes it to the shared terminal atom', async () => {
    mocks.loadedConfig = { terminal: { font_family: '', font_size: 18 } }
    renderTypography()

    const slider = (await screen.findByLabelText('Terminal Font Size')) as HTMLInputElement

    await waitFor(() => expect(slider.value).toBe('18'))
    expect(slider.disabled).toBe(false)
    expect($terminalFontSize.get()).toBe(18)
  })
})
