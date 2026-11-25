import { browserApi, type BrowserBootstrap } from '@/lib/browser-transport'

export interface BrowserTerminalContext {
  hermes_home: string
  shells: { bash: boolean; zsh: boolean }
}

function isBrowserTerminalContext(value: unknown): value is BrowserTerminalContext {
  if (!value || typeof value !== 'object') {
    return false
  }

  const context = value as Partial<BrowserTerminalContext>

  return (
    typeof context.hermes_home === 'string' &&
    Boolean(context.shells && typeof context.shells.bash === 'boolean' && typeof context.shells.zsh === 'boolean')
  )
}

/** Load machine facts needed for the handoff, preserving compatibility with older Webapp servers. */
export async function loadBrowserTerminalContext(bootstrap: BrowserBootstrap, profile: string): Promise<BrowserTerminalContext> {
  try {
    const result = await browserApi<unknown>(bootstrap, { method: 'GET', path: '/api/webapp/terminal-context', profile })

    if (!isBrowserTerminalContext(result)) {
      throw new Error('Invalid Webapp terminal context')
    }

    return result
  } catch (error) {
    // Older servers expose Hermes home on /api/status, but have no shell inventory.
    // Offer only the SSH login shell because bash/zsh availability is unknown.
    if (!(error instanceof Error) || !/^404:/.test(error.message)) {
      throw error
    }

    const status = (await browserApi<unknown>(bootstrap, { method: 'GET', path: '/api/status', profile })) as {
      hermes_home?: unknown
    } | null

    if (!status || typeof status.hermes_home !== 'string') {
      throw new Error('Webapp status did not include Hermes home')
    }

    return { hermes_home: status.hermes_home, shells: { bash: false, zsh: false } }
  }
}
