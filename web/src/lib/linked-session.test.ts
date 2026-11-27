import { describe, expect, it } from 'vitest'

import {
  isLinkedForeignSession,
  linkedForeignAgent,
  linkedResumeBlocked,
  linkedResumeOverlay,
  linkedResumeReadonly,
} from './linked-session'

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
  it('never lets a pending or failed lookup spawn a PTY', () => {
    // Pending: the answer for THIS id has not landed yet.
    expect(linkedResumeBlocked(null, 'a')).toBe(true)
    expect(linkedResumeBlocked({ id: 'b', agent: null, verified: true }, 'a')).toBe(true)
    // Failed lookup: "could not tell" is not "ordinary session".
    expect(linkedResumeBlocked({ id: 'a', agent: null, verified: false }, 'a')).toBe(true)
  })

  it('opens a verified linked row instead of refusing it', () => {
    // The embedded TUI is a canonical client: the gateway serves a linked pointer's native
    // transcript and appends to that same thread, so the row must not be walled off.
    expect(linkedResumeBlocked({ id: 'a', agent: 'claude', verified: true }, 'a')).toBe(false)
    expect(linkedResumeBlocked({ id: 'a', agent: 'codex', verified: true }, 'a')).toBe(false)
    // Ordinary rows are unaffected.
    expect(linkedResumeBlocked({ id: 'a', agent: null, verified: true }, 'a')).toBe(false)
    // No resume target at all: nothing to gate.
    expect(linkedResumeBlocked(null, null)).toBe(false)
  })
})

describe('linkedResumeReadonly', () => {
  it('marks exactly a verified linked row as read-only, and nothing else', () => {
    expect(linkedResumeReadonly({ id: 'a', agent: 'codex', verified: true }, 'a')).toBe(true)
    expect(linkedResumeReadonly({ id: 'a', agent: null, verified: true }, 'a')).toBe(false)
    expect(linkedResumeReadonly({ id: 'a', agent: 'codex', verified: false }, 'a')).toBe(false)
    expect(linkedResumeReadonly({ id: 'b', agent: 'codex', verified: true }, 'a')).toBe(false)
    expect(linkedResumeReadonly(null, 'a')).toBe(false)
    expect(linkedResumeReadonly(null, null)).toBe(false)
  })
})

describe('linkedResumeOverlay', () => {
  it('shows progress, not an empty verdict panel, while the lookup is in flight', () => {
    // Nothing asked yet / wrong id answered / answered for another target: the
    // canvas has no notice to render, so the blocked panel must not take it.
    expect(linkedResumeOverlay(null, 'a')).toBe('loading')
    expect(linkedResumeOverlay({ id: 'b', agent: null, verified: true }, 'a')).toBe('loading')
    expect(linkedResumeOverlay({ id: 'b', agent: 'claude', verified: true }, 'a')).toBe('loading')
  })

  it('reserves the verdict panel for an unverifiable target only', () => {
    expect(linkedResumeOverlay({ id: 'a', agent: null, verified: false }, 'a')).toBe('blocked')
    // A verified linked row opens, so it gets no covering panel at all.
    expect(linkedResumeOverlay({ id: 'a', agent: 'claude', verified: true }, 'a')).toBe('none')
  })

  it('covers nothing when there is no resume target or the row is ordinary', () => {
    expect(linkedResumeOverlay(null, null)).toBe('none')
    expect(linkedResumeOverlay(null, '')).toBe('none')
    expect(linkedResumeOverlay({ id: 'a', agent: null, verified: true }, 'a')).toBe('none')
  })
})
