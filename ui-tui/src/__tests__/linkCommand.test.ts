import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createSlashHandler } from '../app/createSlashHandler.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'

// `/link`: bare opens the linked-thread picker, `/<agent> <thread-id>` links + resumes, and the
// host's own capability answer decides which agents are offered — never an optimistic guess that
// would fail on the next send.

const flush = async () => {
  await new Promise(resolve => setTimeout(resolve, 5))
  await new Promise(resolve => setTimeout(resolve, 5))
}

const LABELS: Record<string, string> = { claude: 'Claude Code', codex: 'Codex', mistral: 'Mistral Vibe' }

const agentRow = (agent: string, writable = true, reason: null | string = null) => ({
  agent,
  cli: { available: true, command: agent, path: null, reason: null, reason_code: null, source: 'path', version: null },
  continue_status: { reason, reason_code: writable ? null : 'store_missing', supported: true, writable },
  label: LABELS[agent],
  store: { available: true, readable: writable, reason, reason_code: writable ? null : 'store_missing' }
})

const status = (rows: ReturnType<typeof agentRow>[]) => ({ agents: rows, host: 'build-host' })

const allLinkable = () => status([agentRow('codex'), agentRow('claude'), agentRow('mistral')])

function harness({
  busy = false,
  link = async () => ({ already_linked: false, session_id: 'linked-sid' }),
  foreignStatus = async () => allLinkable()
}: {
  busy?: boolean
  foreignStatus?: () => Promise<unknown>
  link?: (method: string, params: any) => Promise<unknown>
} = {}) {
  const request = vi.fn(async (method: string, params: any) => {
    if (method === 'session.foreign.status') {
      return foreignStatus()
    }

    if (method.endsWith('.link')) {
      return link(method, params)
    }

    throw Object.assign(new Error(`unknown method: ${method}`), { code: -32601 })
  })

  const sys = vi.fn()
  const resumeById = vi.fn()
  const guardBusySessionSwitch = vi.fn(() => busy)

  const slash = createSlashHandler({
    composer: { enqueue: vi.fn() },
    gateway: { gw: { isCanonical: true, request }, rpc: request },
    local: { getHistoryItems: () => [], getLastUserMsg: () => '', maybeWarn: vi.fn() },
    session: { guardBusySessionSwitch, newSession: vi.fn(), resumeById },
    slashFlightRef: { current: 0 },
    transcript: {
      page: vi.fn(),
      panel: vi.fn(),
      send: vi.fn(),
      setHistoryItems: vi.fn(),
      sys,
      trimLastExchange: (items: unknown[]) => items
    }
  } as any)

  const calls = (method: string) => request.mock.calls.filter(([name]) => name === method)
  const said = () => sys.mock.calls.map(([line]) => String(line))

  return { calls, guardBusySessionSwitch, request, resumeById, said, slash, sys }
}

beforeEach(() => {
  resetUiState()
  resetOverlayState()
  patchUiState({ info: { model: 'm', skills: {}, tools: {} } as any, sid: 'owner' })
})

describe('/link', () => {
  it('opens the picker overlay on the bare form and links nothing', async () => {
    const h = harness()

    h.slash('/link')
    await flush()

    expect(getOverlayState().link).toBe(true)
    expect(h.calls('session.foreign.status')).toHaveLength(0)
    expect(h.resumeById).not.toHaveBeenCalled()
  })

  it('scopes the picker to the agent named without a thread id', async () => {
    harness().slash('/link codex')

    expect(getOverlayState().link).toEqual({ agent: 'codex' })
    expect(getOverlayState().link).not.toBe(true)
  })

  it('links the named Codex thread and resumes the Hermes session it returned', async () => {
    const h = harness()

    h.slash('/link codex 0a1b1c2d')
    await flush()

    expect(h.calls('session.foreign.status')).toHaveLength(1)
    expect(h.calls('session.codex.link')).toEqual([['session.codex.link', { thread_id: '0a1b1c2d' }]])
    expect(h.resumeById).toHaveBeenCalledWith('linked-sid')
    expect(h.said()).not.toContainEqual(expect.stringMatching(/^error: /))
  })

  it('sends Claude Code and Vibe ids as session_id, the parameter their link RPC documents', async () => {
    const claude = harness()

    claude.slash('/link claude 0b2c')
    await flush()
    expect(claude.calls('session.claude.link')).toEqual([['session.claude.link', { session_id: '0b2c' }]])
    expect(claude.resumeById).toHaveBeenCalledWith('linked-sid')

    const vibe = harness()

    vibe.slash('/link mistral 0c3d')
    await flush()
    expect(vibe.calls('session.mistral.link')).toEqual([['session.mistral.link', { session_id: '0c3d' }]])
    expect(vibe.resumeById).toHaveBeenCalledWith('linked-sid')
  })

  it('reports the host reason and links nothing when the agent cannot be continued here', async () => {
    const h = harness({
      foreignStatus: async () =>
        status([
          agentRow('codex', false, 'Codex is not installed on build-host.'),
          agentRow('claude'),
          agentRow('mistral')
        ])
    })

    h.slash('/link codex 0a1b')
    await flush()

    expect(h.said()).toContainEqual('error: Codex is unavailable here: Codex is not installed on build-host.')
    expect(h.calls('session.codex.link')).toHaveLength(0)
    expect(h.resumeById).not.toHaveBeenCalled()
  })

  it('treats an unknown capability as unavailable rather than writable', async () => {
    const h = harness({ foreignStatus: async () => ({ agents: [], host: 'older-build' }) })

    h.slash('/link codex 0a1b')
    await flush()

    expect(h.said()).toContainEqual('error: Codex is unavailable here: This gateway did not report Codex capability.')
    expect(h.calls('session.codex.link')).toHaveLength(0)
  })

  it('resolves a lone thread id when exactly one agent on this host is linkable', async () => {
    const h = harness({
      foreignStatus: async () =>
        status([
          agentRow('codex'),
          agentRow('claude', false, 'No Claude Code store here.'),
          agentRow('mistral', false, 'No Vibe store here.')
        ])
    })

    h.slash('/link 0d4e')
    await flush()

    expect(h.calls('session.codex.link')).toEqual([['session.codex.link', { thread_id: '0d4e' }]])
    expect(h.resumeById).toHaveBeenCalledWith('linked-sid')
  })

  it('asks for the agent when a lone thread id could belong to more than one', async () => {
    const h = harness()

    h.slash('/link 0d4e')
    await flush()

    expect(h.said()).toContainEqual(
      'error: name the agent — /link <agent> 0d4e (this host can serve codex, claude, mistral)'
    )
    expect(h.request.mock.calls.filter(([method]) => method.endsWith('.link'))).toHaveLength(0)
    expect(h.resumeById).not.toHaveBeenCalled()
  })

  it('says no agent can serve a lone thread id when none can', async () => {
    const h = harness({
      foreignStatus: async () =>
        status([
          agentRow('codex', false, 'Codex has no session store on build-host.'),
          agentRow('claude', false, 'Claude Code is not installed on build-host.'),
          agentRow('mistral', false, 'Mistral is not installed on build-host.')
        ])
    })

    h.slash('/link 0d4e')
    await flush()

    expect(h.said()).toContainEqual(
      'error: no agent on this host can open a linked thread (Codex: Codex has no session store on ' +
        'build-host.; Claude Code: Claude Code is not installed on build-host.; ' +
        'Mistral Vibe: Mistral is not installed on build-host.)'
    )
    expect(h.resumeById).not.toHaveBeenCalled()
  })

  it('surfaces a failed capability probe as an error line instead of linking', async () => {
    const h = harness({
      foreignStatus: () => Promise.reject(new Error('gateway socket closed'))
    })

    h.slash('/link codex 0a1b')
    await flush()

    expect(h.said()).toHaveLength(1)
    expect(h.said()[0]).toMatch(/^error: /)
    expect(h.calls('session.codex.link')).toHaveLength(0)
    expect(h.resumeById).not.toHaveBeenCalled()
  })

  it('surfaces a refused link as an error line and resumes nothing', async () => {
    const h = harness({
      link: () => Promise.reject(Object.assign(new Error('busy'), { code: 4009 }))
    })

    h.slash('/link codex 0a1b')
    await flush()

    expect(h.said()).toHaveLength(1)
    expect(h.said()[0]).toMatch(/^error: /)
    expect(h.resumeById).not.toHaveBeenCalled()
  })

  it('refuses to start a link while a turn is running', async () => {
    const h = harness({ busy: true })

    h.slash('/link codex 0a1b')
    await flush()

    expect(h.guardBusySessionSwitch).toHaveBeenCalledWith('link a native thread')
    expect(h.calls('session.foreign.status')).toHaveLength(0)
    expect(h.calls('session.codex.link')).toHaveLength(0)
    expect(h.resumeById).not.toHaveBeenCalled()
  })

  it('reports an already-linked thread without changing the resume target', async () => {
    const h = harness({ link: async () => ({ already_linked: true, session_id: 'existing-sid' }) })

    h.slash('/link codex 0a1b')
    await flush()

    expect(h.said()).toContainEqual('Codex thread 0a1b was already linked — opening it')
    expect(h.resumeById).toHaveBeenCalledWith('existing-sid')
  })
})
