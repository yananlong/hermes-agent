import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetHostCapabilityCache as resetHostCapabilityCacheFromStore } from '@/store/host-capability-cache'

import type { ForeignAgentCapability, ForeignStatus } from './api'
import { foreignStatusRequest } from './api'
import {
  agentHostCapability,
  continueReason,
  type HostCapability,
  hostCapabilityScope,
  isLinkedThreadWritable,
  linkBlockedReason,
  readHostCapability,
  resetHostCapabilityCache
} from './capability'

vi.mock('./api', () => ({ foreignStatusRequest: vi.fn() }))

const owner = { connectionId: 'workstation', profile: 'research' }

function methodNotFound() {
  return Object.assign(new Error('unknown method: session.foreign.status'), { code: -32601 })
}

function row(agent: 'codex' | 'claude' | 'mistral', overrides: Partial<ForeignAgentCapability> = {}): ForeignAgentCapability {
  return {
    agent,
    label: agent,
    store: { available: true, readable: true, reason_code: null, reason: null },
    cli: { available: true, command: agent, path: null, version: null, source: 'path', reason_code: null, reason: null },
    continue_status: { supported: true, writable: false, reason_code: 'send_read_only', reason: 'read-only here' },
    ...overrides
  }
}

function ready(agents: ForeignAgentCapability[]): HostCapability {
  const status: ForeignStatus = { host: 'studio', agents }

  return { state: 'ready', status }
}

beforeEach(() => {
  resetHostCapabilityCache()
})

afterEach(() => {
  vi.mocked(foreignStatusRequest).mockReset()
})

describe('version skew: an older gateway', () => {
  it('treats method-not-found as unknown capability, never as writable', async () => {
    vi.mocked(foreignStatusRequest).mockRejectedValue(methodNotFound())

    const capability = await readHostCapability(owner)

    expect(capability).toEqual({ state: 'unknown' })
    // Unknown is the read-only fallback: no agent is writable and no link is blocked.
    expect(isLinkedThreadWritable(capability, 'claude')).toBe(false)
    expect(continueReason(capability, 'claude')).toBe('capability_unknown')
    expect(linkBlockedReason(capability, 'codex')).toBeNull()
  })

  it('caches the negative result per serving scope and stops dialing', async () => {
    vi.mocked(foreignStatusRequest).mockRejectedValue(methodNotFound())

    await readHostCapability(owner)
    await readHostCapability(owner)
    await readHostCapability({ connectionId: 'workstation', profile: 'research' })

    expect(foreignStatusRequest).toHaveBeenCalledTimes(1)

    // Another backend serving another profile is a different question.
    await readHostCapability({ connectionId: 'laptop', profile: 'research' })
    expect(foreignStatusRequest).toHaveBeenCalledTimes(2)
  })

  it('re-arms after the cache is cleared', async () => {
    vi.mocked(foreignStatusRequest).mockRejectedValue(methodNotFound())
    await readHostCapability(owner)

    resetHostCapabilityCache()
    vi.mocked(foreignStatusRequest).mockResolvedValue({ host: 'studio', agents: [row('claude')] })
    const capability = await readHostCapability(owner)

    expect(capability.state).toBe('ready')
    expect(foreignStatusRequest).toHaveBeenCalledTimes(2)
  })

  it('shares one cache with the connection lifecycle that clears it', async () => {
    // `store/gateway.ts` calls this exact function when a socket opens, so clearing through the
    // store-side module has to invalidate the verdict this module cached — otherwise an upgraded
    // gateway keeps answering from the old build's -32601 until the window is relaunched.
    vi.mocked(foreignStatusRequest).mockRejectedValue(methodNotFound())
    await readHostCapability(owner)
    await readHostCapability(owner)
    expect(foreignStatusRequest).toHaveBeenCalledTimes(1)

    resetHostCapabilityCacheFromStore()

    await readHostCapability(owner)
    expect(foreignStatusRequest).toHaveBeenCalledTimes(2)
  })
})

describe('a failed probe', () => {
  it('reports error (retryable) and is not cached as unsupported', async () => {
    vi.mocked(foreignStatusRequest).mockRejectedValueOnce(new Error('socket closed'))
    expect(await readHostCapability(owner)).toEqual({ state: 'error', message: 'socket closed' })

    vi.mocked(foreignStatusRequest).mockResolvedValue({ host: 'studio', agents: [row('claude')] })
    expect((await readHostCapability(owner)).state).toBe('ready')
    expect(foreignStatusRequest).toHaveBeenCalledTimes(2)
  })

  it('does not dial at all when the caller already aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    vi.mocked(foreignStatusRequest).mockRejectedValue(new Error('aborted'))

    expect(await readHostCapability(owner, controller.signal)).toEqual({ state: 'unknown' })
  })
})

describe('link affordances are per agent', () => {
  it('blocks codex when its store OR CLI is missing on the host', () => {
    const noCli = ready([row('codex', {
      cli: { available: false, command: 'codex', path: null, version: null, source: 'path', reason_code: 'cli_missing', reason: 'not installed' }
    })])

    const noStore = ready([row('codex', {
      store: { available: false, readable: false, reason_code: 'store_missing', reason: 'no store' }
    })])

    expect(linkBlockedReason(noCli, 'codex')).toBe('cli_missing')
    expect(linkBlockedReason(noStore, 'codex')).toBe('store_missing')
    expect(linkBlockedReason(ready([row('codex')]), 'codex')).toBeNull()
  })

  it('does not block a Claude link on a missing claude CLI: linking reads the store', () => {
    const claude = ready([row('claude', {
      cli: { available: false, command: 'claude', path: null, version: null, source: 'path', reason_code: 'cli_missing', reason: 'not installed' }
    })])

    expect(linkBlockedReason(claude, 'claude')).toBeNull()
  })

  it('does not block anything while the answer is unknown', () => {
    expect(linkBlockedReason(undefined, 'mistral')).toBeNull()
    expect(linkBlockedReason({ state: 'error', message: 'x' }, 'mistral')).toBeNull()
  })
})

describe('writable continue', () => {
  it('is writable only for an explicit writable:true row', () => {
    const writable = ready([row('claude', {
      continue_status: { supported: true, writable: true, reason_code: null, reason: null }
    })])

    expect(isLinkedThreadWritable(writable, 'claude')).toBe(true)
    expect(continueReason(writable, 'claude')).toBeNull()
    expect(isLinkedThreadWritable(ready([row('claude')]), 'claude')).toBe(false)
  })

  it('reports the gateway reason code when the host is not writable', () => {
    const build = ready([row('mistral', {
      continue_status: { supported: false, writable: false, reason_code: 'build_unsupported', reason: 'nope' }
    })])

    const scope = ready([row('mistral', {
      store: { available: false, readable: false, reason_code: 'profile_scope', reason: 'nope' },
      continue_status: { supported: true, writable: false, reason_code: 'profile_scope', reason: 'nope' }
    })])

    expect(continueReason(build, 'mistral')).toBe('build_unsupported')
    expect(continueReason(scope, 'mistral')).toBe('profile_scope')
  })

  it('never unlocks an agent the answer does not name', () => {
    expect(isLinkedThreadWritable(ready([row('codex')]), 'claude')).toBe(false)
    expect(continueReason(ready([row('codex')]), 'claude')).toBe('capability_unknown')
  })
})

describe('scope', () => {
  it('keys the cache by connection and profile', () => {
    expect(hostCapabilityScope({ connectionId: 'a', profile: 'default' })).not.toBe(
      hostCapabilityScope({ connectionId: 'a', profile: 'research' })
    )
    expect(agentHostCapability(ready([row('claude')]), 'claude')?.agent).toBe('claude')
    expect(agentHostCapability(undefined, 'claude')).toBeNull()
  })
})
