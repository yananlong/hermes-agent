// @vitest-environment jsdom
import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  $terminalFontFamily,
  $terminalFontSize,
  setTerminalFontFamilyFromConfig,
  setTerminalFontSizeFromConfig
} from './terminal-font'
import { useTerminalFontController } from './use-terminal-font'

const mocks = vi.hoisted(() => ({ redrawAllTerminals: vi.fn() }))

vi.mock('./terminals', () => ({ redrawAllTerminals: mocks.redrawAllTerminals }))

describe('useTerminalFontController', () => {
  afterEach(() => {
    setTerminalFontFamilyFromConfig('')
    setTerminalFontSizeFromConfig(11)
    mocks.redrawAllTerminals.mockClear()
  })

  it('repaints and refits an already-mounted xterm when profile font settings change', async () => {
    const term = {
      options: { fontFamily: 'fallback', fontSize: 11 },
      refresh: vi.fn(),
      rows: 18
    }

    const fit = vi.fn()

    const clearTextureAtlas = vi.fn()

    const refs = {
      fitRef: { current: fit },
      termRef: { current: term },
      webglRef: { current: { clearTextureAtlas } }
    }

    const { result } = renderHook(() =>
      useTerminalFontController(refs as unknown as Parameters<typeof useTerminalFontController>[0])
    )

    result.current.mountedRef.current = true
    act(() => {
      $terminalFontFamily.set('MesloLGS NF')
      $terminalFontSize.set(16)
    })

    await waitFor(() => {
      expect(term.options.fontFamily).toContain('MesloLGS NF')
      expect(term.options.fontSize).toBe(16)
      expect(mocks.redrawAllTerminals).toHaveBeenCalledOnce()
    })
    expect(fit).toHaveBeenCalledOnce()
    expect(clearTextureAtlas).toHaveBeenCalledOnce()
    expect(term.refresh).toHaveBeenCalledWith(0, 17)
    expect(mocks.redrawAllTerminals).toHaveBeenCalledWith()
    expect(clearTextureAtlas.mock.invocationCallOrder[0]).toBeLessThan(
      mocks.redrawAllTerminals.mock.invocationCallOrder[0]
    )
  })

  it('tracks the latest profile size before xterm mounts', () => {
    const refs = {
      fitRef: { current: vi.fn() },
      termRef: { current: null },
      webglRef: { current: null }
    }

    const { result } = renderHook(() =>
      useTerminalFontController(refs as unknown as Parameters<typeof useTerminalFontController>[0])
    )

    act(() => $terminalFontSize.set(20))

    expect(result.current.latestFontSizeRef.current).toBe(20)
  })

  it('applies shared size changes to every mounted terminal pane', async () => {
    const terminalRefs = Array.from({ length: 2 }, (_, index) => {
      const term = {
        options: { fontFamily: 'fallback', fontSize: 11 },
        refresh: vi.fn(),
        rows: 12 + index
      }

      const fit = vi.fn()

      const clearTextureAtlas = vi.fn()

      const refs = {
        fitRef: { current: fit },
        termRef: { current: term },
        webglRef: { current: { clearTextureAtlas } }
      }

      return { clearTextureAtlas, fit, refs, term }
    })

    const panes = terminalRefs.map(({ refs }) =>
      renderHook(() => useTerminalFontController(refs as unknown as Parameters<typeof useTerminalFontController>[0]))
    )

    panes.forEach(pane => {
      pane.result.current.mountedRef.current = true
    })
    act(() => $terminalFontSize.set(19))

    await waitFor(() => {
      terminalRefs.forEach(({ term }) => expect(term.options.fontSize).toBe(19))
    })

    terminalRefs.forEach(({ clearTextureAtlas, fit, term }, index) => {
      expect(fit).toHaveBeenCalledOnce()
      expect(clearTextureAtlas).toHaveBeenCalledOnce()
      expect(term.refresh).toHaveBeenCalledWith(0, 11 + index)
    })
    expect(mocks.redrawAllTerminals).toHaveBeenCalled()
    panes.forEach(pane => pane.unmount())
  })
})
