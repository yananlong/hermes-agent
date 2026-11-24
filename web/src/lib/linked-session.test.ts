import { describe, expect, it } from 'vitest'

import { isLinkedForeignSession, linkedForeignAgent, linkedResumeBlocked } from './linked-session'

describe('linkedForeignAgent', () => {
  it('returns null for ordinary sessions and missing rows', () => {
    expect(linkedForeignAgent(null)).toBeNull()
    expect(linkedForeignAgent(undefined)).toBeNull()
    expect(linkedForeignAgent({})).toBeNull()
    expect(
      linkedForeignAgent({ codex_linked: false, claude_linked: false, mistral_linked: false })
    ).toBeNull()
  })

  it('detects each native owner from its list-payload flag', () => {
    expect(linkedForeignAgent({ codex_linked: true })).toBe('codex')
    expect(linkedForeignAgent({ claude_linked: true })).toBe('claude')
    expect(linkedForeignAgent({ mistral_linked: true })).toBe('mistral')
  })

  it('treats non-boolean flag values as unlinked', () => {
    expect(linkedForeignAgent({ codex_linked: 1 as unknown as boolean })).toBeNull()
  })
})

describe('isLinkedForeignSession', () => {
  it('is true exactly when a linked flag is set', () => {
    expect(isLinkedForeignSession({ claude_linked: true })).toBe(true)
    expect(isLinkedForeignSession({})).toBe(false)
    expect(isLinkedForeignSession(undefined)).toBe(false)
  })
})

describe('linkedResumeBlocked', () => {
  it('blocks a linked row and never lets a pending or failed lookup spawn a PTY', () => {
    expect(linkedResumeBlocked({ id: 'a', agent: 'claude', verified: true }, 'a')).toBe(true)
    // Pending: the answer for THIS id has not landed yet.
    expect(linkedResumeBlocked(null, 'a')).toBe(true)
    expect(linkedResumeBlocked({ id: 'b', agent: null, verified: true }, 'a')).toBe(true)
    // Failed lookup: "could not tell" is not "ordinary session".
    expect(linkedResumeBlocked({ id: 'a', agent: null, verified: false }, 'a')).toBe(true)
  })

  it('allows the ordinary resume path only for a verified non-pointer row', () => {
    expect(linkedResumeBlocked({ id: 'a', agent: null, verified: true }, 'a')).toBe(false)
    // No resume target at all: nothing to gate.
    expect(linkedResumeBlocked(null, null)).toBe(false)
  })
})
