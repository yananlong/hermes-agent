import { beforeEach, describe, expect, it, vi } from 'vitest'

const { browserApi } = vi.hoisted(() => ({ browserApi: vi.fn() }))

vi.mock('@/lib/browser-transport', () => ({ browserApi }))

import { loadBrowserTerminalContext } from './browser-terminal-context'

const bootstrap = { authRequired: false, basePath: '', privateSession: false, token: '' }

describe('loadBrowserTerminalContext', () => {
  beforeEach(() => browserApi.mockReset())

  it('uses only the SSH login shell when an older server lacks shell inventory', async () => {
    browserApi.mockRejectedValueOnce(new Error('404: Not Found')).mockResolvedValueOnce({
      hermes_home: '/srv/hermes/.hermes'
    })

    await expect(loadBrowserTerminalContext(bootstrap)).resolves.toEqual({
      hermes_home: '/srv/hermes/.hermes',
      shells: { bash: false, zsh: false }
    })
    expect(browserApi.mock.calls.map(([, request]) => request.path)).toEqual([
      '/api/webapp/terminal-context',
      '/api/status'
    ])
  })

  it('retains shell availability from the current endpoint', async () => {
    const context = { hermes_home: '/srv/hermes/.hermes', shells: { bash: true, zsh: false } }
    browserApi.mockResolvedValueOnce(context)

    await expect(loadBrowserTerminalContext(bootstrap)).resolves.toEqual(context)
    expect(browserApi).toHaveBeenCalledTimes(1)
  })

  it('does not turn other endpoint failures into a legacy fallback', async () => {
    browserApi.mockRejectedValueOnce(new Error('503: Unavailable'))

    await expect(loadBrowserTerminalContext(bootstrap)).rejects.toThrow('503: Unavailable')
    expect(browserApi).toHaveBeenCalledTimes(1)
  })

  it('rejects a legacy status response without Hermes home', async () => {
    browserApi.mockRejectedValueOnce(new Error('404: Not Found')).mockResolvedValueOnce({})

    await expect(loadBrowserTerminalContext(bootstrap)).rejects.toThrow('Webapp status did not include Hermes home')
  })
})
