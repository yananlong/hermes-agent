import { describe, expect, it, vi } from 'vitest'

import { linkedHistoryPage, LinkedHistoryWindow, probeLinkedHistory, type LinkedHistoryPage } from '../app/linkedHistory.js'

const row = (text: string) => ({ role: 'user', text })

describe('linked history window', () => {
  it('keeps a bounded latest window and reloads evicted pages in either direction', async () => {
    const pages: Record<string, LinkedHistoryPage> = {
      latest: { session_id: 's', linked_codex: true, reset: true, rows: [row('new '.repeat(20))], next_cursor: 'c1' },
      c1: { session_id: 's', linked_codex: true, reset: false, rows: [row('middle '.repeat(10))], next_cursor: 'c2' },
      c2: { session_id: 's', linked_codex: true, reset: false, rows: [row('old')], next_cursor: null }
    }
    const request = vi.fn(async (_method, params) => pages[params.cursor ?? 'latest']) as any
    const first = await probeLinkedHistory(request, 's')
    expect(first).not.toBeNull()
    const window = new LinkedHistoryWindow(request, 's')
    const budget = new TextEncoder().encode(JSON.stringify(first!.rows)).byteLength + 1
    await window.loadInitial(first!, budget)
    expect(window.rows).toEqual([row('new '.repeat(20))])
    await window.loadOlder()
    expect(window.hasNewer).toBe(true)
    await window.loadOlder()
    expect(window.rows).toEqual([row('old')])
    expect(window.cursor).toBeNull()
    await window.loadNewer()
    expect(window.rows).toEqual([row('middle '.repeat(10))])
    await window.loadNewer()
    expect(window.rows).toEqual([row('new '.repeat(20))])
    expect(window.bytes).toBeLessThanOrEqual(budget)
    expect(request).toHaveBeenCalledWith('session.transcript', expect.objectContaining({ session_id: 's', order: 'latest' }))
  })

  it('refuses a cross-session page, a nonadvancing cursor, and changed evicted content', async () => {
    const bad = vi.fn(async () => ({ session_id: 'other', rows: [], reset: true, next_cursor: null })) as any
    await expect(linkedHistoryPage(bad, 's')).rejects.toThrow('invalid')
    const stuck = vi.fn(async () => ({ session_id: 's', linked_codex: true, rows: [], reset: false, next_cursor: 'c' })) as any
    await expect(linkedHistoryPage(stuck, 's', 'c')).rejects.toThrow('invalid')

    let changed = false
    const request = vi.fn(async (_method, params) => params.cursor
      ? { session_id: 's', linked_codex: true, rows: [row('older'.repeat(20))], reset: false, next_cursor: null }
      : { session_id: 's', linked_codex: true, rows: [row(changed ? 'changed' : 'new'.repeat(20))], reset: true, next_cursor: 'c' }) as any
    const window = new LinkedHistoryWindow(request, 's')
    const first = await linkedHistoryPage(request, 's')
    await window.loadInitial(first, 1)
    await window.loadOlder()
    changed = true
    await expect(window.loadNewer()).rejects.toThrow('changed')
  })
})
