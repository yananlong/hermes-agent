import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/hermes'

import { isLinkedForeignSession, linkedForeignAgent } from './linked-session'

const row = (extra: Partial<SessionInfo>): SessionInfo => ({ id: 's', ...extra }) as SessionInfo

describe('linkedForeignAgent', () => {
  it('is null for ordinary and missing sessions', () => {
    expect(linkedForeignAgent(undefined)).toBeNull()
    expect(linkedForeignAgent(null)).toBeNull()
    expect(linkedForeignAgent(row({}))).toBeNull()
    expect(linkedForeignAgent(row({ codex_linked: false }))).toBeNull()
  })

  it.each([
    [{ codex_linked: true }, 'codex'],
    [{ claude_linked: true }, 'claude'],
    [{ mistral_linked: true }, 'mistral']
  ] as const)('maps %o to %s', (flags, agent) => {
    expect(linkedForeignAgent(row(flags))).toBe(agent)
    expect(isLinkedForeignSession(row(flags))).toBe(true)
  })

  it('prefers codex when several flags are set', () => {
    expect(linkedForeignAgent(row({ claude_linked: true, codex_linked: true }))).toBe('codex')
  })
})
