/** A display-only window over the gateway's owner-authorized native transcript. */
import { createHash } from 'node:crypto'

import { asRpcResult } from '../lib/rpc.js'

export const LINKED_HISTORY_INITIAL_BYTES = 8 * 1024 * 1024
const PAGE_ROWS = 500
const MAX_PAGE_MARKERS = 8192
const MAX_INITIAL_PAGES = 32

export interface LinkedHistoryPage {
  linked_codex?: boolean
  next_cursor: null | string
  reset: boolean
  rows: Record<string, unknown>[]
  session_id: string
}

export type HistoryRequest = <T>(method: string, params: Record<string, unknown>) => Promise<T>

function validatePage(response: LinkedHistoryPage | null, sessionId: string, cursor?: string): LinkedHistoryPage {
  if (
    !response || response.session_id !== sessionId || !Array.isArray(response.rows) ||
    typeof response.reset !== 'boolean' ||
    !(response.next_cursor === null || typeof response.next_cursor === 'string') ||
    response.rows.some(row => !row || typeof row !== 'object' || Array.isArray(row)) ||
    (cursor !== undefined && response.next_cursor === cursor)
  ) {
    throw new Error('The linked history page was invalid')
  }

  return response
}

export async function probeLinkedHistory(request: HistoryRequest, sessionId: string): Promise<LinkedHistoryPage | null> {
  const response = asRpcResult<LinkedHistoryPage>(
    await request('session.transcript', { limit: PAGE_ROWS, order: 'latest', session_id: sessionId })
  )

  return response?.linked_codex === true ? validatePage(response, sessionId) : null
}

export async function linkedHistoryPage(request: HistoryRequest, sessionId: string, cursor?: string): Promise<LinkedHistoryPage> {
  const response = asRpcResult<LinkedHistoryPage>(
    await request('session.transcript', {
      limit: PAGE_ROWS,
      order: 'latest',
      session_id: sessionId,
      ...(cursor ? { cursor } : {})
    })
  )

  const page = validatePage(response, sessionId, cursor)
  if (page.linked_codex !== true) throw new Error('The linked history identity changed')
  return page
}

interface WindowPage {
  after: null | string
  before: null | string
  digest: string
  rows: Record<string, unknown>[]
  size: number
}

type PageMarker = Omit<WindowPage, 'rows'>

function windowPage(before: null | string, response: LinkedHistoryPage): WindowPage {
  const json = JSON.stringify(response.rows)
  return { after: response.next_cursor, before, digest: createHash('sha256').update(json).digest('hex'),
    rows: response.rows, size: new TextEncoder().encode(json).byteLength }
}

export class LinkedHistoryWindow {
  private pages: WindowPage[] = []
  private newer: PageMarker[] = []
  private budget = LINKED_HISTORY_INITIAL_BYTES
  bytes = 0
  lastAdded: Record<string, unknown>[] = []
  lastEvicted: Record<string, unknown>[] = []
  newerLost = false

  constructor(readonly request: HistoryRequest, readonly sessionId: string) {}

  get rows() { return this.pages.flatMap(page => page.rows) }
  get cursor() { return this.pages[0]?.after ?? null }
  get hasNewer() { return this.newer.length > 0 }

  async loadInitial(first: LinkedHistoryPage, maxBytes = LINKED_HISTORY_INITIAL_BYTES) {
    let response = first
    let before: null | string = null

    this.pages = []
    this.newer = []
    this.bytes = 0
    this.budget = maxBytes
    this.newerLost = false
    let acceptedPage = false
    const seen = new Set<null | string>()

    for (let pageCount = 0; pageCount < MAX_INITIAL_PAGES; pageCount++) {
      if (seen.has(before)) throw new Error('Linked history returned a repeating cursor')
      seen.add(before)
      const page = windowPage(before, response)

      if (acceptedPage && this.bytes + page.size > maxBytes) {
        break
      }

      this.pages.unshift(page)
      this.bytes += page.size
      acceptedPage = true

      if (!page.after || this.bytes >= maxBytes || pageCount + 1 === MAX_INITIAL_PAGES) {
        break
      }

      before = page.after
      response = await linkedHistoryPage(this.request, this.sessionId, before)

      if (response.reset) {
        throw new Error('Linked history changed while loading; reopen the session')
      }
    }

    return this.rows
  }

  async loadOlder() {
    if (!this.cursor) {
      return []
    }

    const before = this.cursor
    const response = await linkedHistoryPage(this.request, this.sessionId, before)

    if (response.reset) {
      throw new Error('Linked history changed; reopen the session to refresh it')
    }

    const page = windowPage(before, response)
    const boundaries = new Set([...this.pages, ...this.newer].map(item => item.before))
    if (page.after !== null && boundaries.has(page.after)) throw new Error('Linked history returned a repeating cursor')
    this.pages.unshift(page)
    this.bytes += page.size
    this.lastAdded = page.rows
    this.lastEvicted = []

    while (this.bytes > this.budget && this.pages.length > 1) {
      const removed = this.pages.pop()!
      const { rows, ...marker } = removed
      this.bytes -= removed.size
      this.lastEvicted = [...rows, ...this.lastEvicted]
      this.newer.push(marker)
      if (this.newer.length > MAX_PAGE_MARKERS) {
        this.newer.shift()
        this.newerLost = true
      }
    }

    return page.rows
  }

  async loadNewer() {
    const marker = this.newer.at(-1)
    if (!marker) {
      return []
    }
    const response = await linkedHistoryPage(this.request, this.sessionId, marker.before ?? undefined)
    const page = windowPage(marker.before, response)
    if ((marker.before !== null && response.reset) || page.after !== marker.after || page.digest !== marker.digest) {
      throw new Error('Linked history changed; reopen the session to refresh it')
    }
    this.newer.pop()
    this.pages.push(page)
    this.bytes += page.size
    this.lastAdded = page.rows
    this.lastEvicted = []
    while (this.bytes > this.budget && this.pages.length > 1) {
      const removed = this.pages.shift()!
      this.bytes -= removed.size
      this.lastEvicted.push(...removed.rows)
    }
    return page.rows
  }
}
