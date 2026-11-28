import { beforeEach, describe, expect, it, vi } from 'vitest'

import { toChatMessages } from '@/lib/chat-messages'
import { clearTranscriptTailPaging, recordTranscriptTail, transcriptTailState } from '@/store/transcript-tail'
import type { SessionMessage } from '@/types/hermes'

vi.mock('@/api/native-session-history', () => ({ readNativeTranscriptPage: vi.fn() }))
const { readNativeTranscriptPage } = await import('@/api/native-session-history')
const { backfillOlderTranscriptPage, graftRefreshedTailOntoBackfill, mergeOlderTranscriptPage } = await import('./transcript-backfill')

const owner = { connectionId: 'remote-a', profile: 'work' }
const row = (id: string, text = id): SessionMessage => ({ role: 'user', content: text, timestamp: 1000, codex_native_row_id: id })
const page = (rows: SessionMessage[], cursor: string | null, reset = false) => ({
  session_id: 'thread', profile: 'work', messages: rows,
  native_history: { identity: 'identity-a', next_cursor: cursor, reset },
  pagination: { limit: 100, offset: 0, order: 'latest' as const, returned: rows.length }
})

beforeEach(() => {
  clearTranscriptTailPaging()
  vi.mocked(readNativeTranscriptPage).mockReset()
})

describe('native history paging', () => {
  it('keeps distinct native rows even when timestamps and text repeat across pages', () => {
    const nativeRow = (id: string): SessionMessage => ({
      role: 'user', content: 'same question', timestamp: 1000, codex_native_row_id: id
    })
    const older = toChatMessages([nativeRow('turn-a:item-a'), nativeRow('turn-b:item-b')])
    const newest = toChatMessages([nativeRow('turn-c:item-c'), nativeRow('turn-d:item-d')])
    const merged = mergeOlderTranscriptPage(newest, older)

    expect(merged).toHaveLength(4)
    expect(new Set(merged.map(message => message.id)).size).toBe(4)
  })

  it('follows the captured owner and native cursor, and preserves policy notices and hidden rows', async () => {
    const newest = page([row('new')], 'older')
    recordTranscriptTail('thread', newest, owner, owner)
    const hidden = { ...row('private', 'do not show'), display_kind: 'hidden' }
    const notice: SessionMessage = { role: 'system', content: 'Earlier output is hidden', codex_native_row_id: 'notice' }
    vi.mocked(readNativeTranscriptPage).mockResolvedValue(page([hidden, notice, row('old')], null))
    let messages = toChatMessages(newest.messages)
    await backfillOlderTranscriptPage({
      storedSessionId: 'thread', profile: owner, isCurrent: () => true, heldMessages: () => messages,
      applyOlderPage: older => { messages = mergeOlderTranscriptPage(messages, older) }
    })
    expect(readNativeTranscriptPage).toHaveBeenCalledWith('thread', owner, 'older')
    expect(messages.map(message => message.id)).toEqual(['codex:notice', 'codex:old', 'codex:new'])
    expect(transcriptTailState('thread', owner)?.possiblyTruncated).toBe(false)
  })

  it('keeps the previous cursor across refresh and rejects an invalidated page without prepending', async () => {
    recordTranscriptTail('thread', page([row('new')], 'old-cursor'), owner, owner)
    recordTranscriptTail('thread', page([row('new'), row('newer')], 'fresh-cursor'), owner, owner)
    expect(transcriptTailState('thread', owner)?.native?.cursor).toBe('old-cursor')
    vi.mocked(readNativeTranscriptPage).mockResolvedValue(page([row('replacement')], 'other', true))
    const apply = vi.fn()
    await backfillOlderTranscriptPage({ storedSessionId: 'thread', profile: owner, isCurrent: () => true, applyOlderPage: apply })
    expect(apply).not.toHaveBeenCalled()
    expect(transcriptTailState('thread', owner)?.native?.error).toBe('changed')
  })

  it('keeps the cursor and marks the failure for a retry', async () => {
    recordTranscriptTail('thread', page([row('new')], 'old-cursor'), owner, owner)
    vi.mocked(readNativeTranscriptPage).mockRejectedValue(new Error('offline'))
    await backfillOlderTranscriptPage({ storedSessionId: 'thread', profile: owner, isCurrent: () => true, applyOlderPage: vi.fn() })
    expect(transcriptTailState('thread', owner)?.native).toMatchObject({ cursor: 'old-cursor', error: 'unavailable' })
  })

  it('retains paged history when appended turns appear in the refreshed page', () => {
    const old = toChatMessages([row('old'), row('a'), row('b')])
    const refresh = toChatMessages([row('a'), row('b'), row('c')])
    expect(graftRefreshedTailOntoBackfill(refresh, old).map(message => message.id))
      .toEqual(['codex:old', 'codex:a', 'codex:b', 'codex:c'])
  })

  it('asks for reload if a refreshed tail no longer overlaps and reload replaces the invalid cursor', async () => {
    recordTranscriptTail('thread', page([row('a')], 'old-cursor'), owner, owner)
    recordTranscriptTail('thread', page([row('rewritten')], 'new-cursor'), owner, owner)
    expect(transcriptTailState('thread', owner)?.native?.error).toBe('changed')
    vi.mocked(readNativeTranscriptPage).mockResolvedValue(page([row('rewritten')], 'new-cursor', true))
    const reset = vi.fn()
    await backfillOlderTranscriptPage({ storedSessionId: 'thread', profile: owner, isCurrent: () => true,
      applyOlderPage: vi.fn(), applyResetPage: reset })
    expect(reset.mock.calls[0][0].map((message: { id: string }) => message.id)).toEqual(['codex:rewritten'])
    expect(transcriptTailState('thread', owner)?.native).toMatchObject({ cursor: 'new-cursor' })
    expect(transcriptTailState('thread', owner)?.native?.error).toBeUndefined()
  })
})
