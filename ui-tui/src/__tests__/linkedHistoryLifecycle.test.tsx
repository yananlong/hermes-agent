import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { PassThrough } from 'node:stream'

import { renderSync, Text } from '@hermes/ink'
import React from 'react'
import { expect, it, vi } from 'vitest'

import { getUiState, resetUiState } from '../app/uiStore.js'
import { useSessionLifecycle } from '../app/useSessionLifecycle.js'
import type { Msg } from '../types.js'

it('resumes a linked native window and fetches older pages without rendering the stale mirror', async () => {
  const home = mkdtempSync(join(tmpdir(), 'ink-linked-history-'))
  vi.stubEnv('HERMES_HOME', home)
  resetUiState()
  const body = 'x'.repeat(1_800_000)
  const page = (index: number) => ({ session_id: 'linked', linked_codex: true,
    rows: [{ role: 'user', text: `native-${index} ${body}` }],
    next_cursor: index === 5 ? null : `c${index + 1}`, reset: index === 0 })
  const request = vi.fn(async (method: string, params: any) => {
    if (method === 'session.transcript') return page(params.cursor ? Number(params.cursor.slice(1)) : 0)
    if (method === 'session.resume') return { session_id: 'linked', stored_session_id: 'linked',
      subscription_id: 'sub', authority_epoch: 1, execution_generation: 0, linked_codex: true,
      messages: [{ role: 'user', text: 'STALE local mirror' }], running: false,
      info: { model: 'test', tools: {}, skills: {} } }
    return {}
  })
  let lifecycle!: ReturnType<typeof useSessionLifecycle>
  let messages: Msg[] = []
  const setHistoryItems = (value: Msg[] | ((previous: Msg[]) => Msg[])) => {
    messages = typeof value === 'function' ? value(messages) : value
  }
  const linkedMode = vi.fn()
  const sys = vi.fn()

  function Harness() {
    lifecycle = useSessionLifecycle({ colsRef: { current: 80 }, composerActions: { setComposerTokens: vi.fn() },
      gw: { isCanonical: true, request }, rpc: request, scrollRef: { current: null }, panel: vi.fn(), sys,
      setHistoryItems, setLinkedHistoryMode: linkedMode, setLastUserMsg: vi.fn(), setSessionStartedAt: vi.fn(),
      setStickyPrompt: vi.fn(), setVoiceProcessing: vi.fn(), setVoiceRecording: vi.fn() } as any)
    return <Text>linked history</Text>
  }

  const stream = () => Object.assign(new PassThrough(), { columns: 80, rows: 20, isTTY: false })
  const instance = renderSync(<Harness />, { stdin: stream() as any, stdout: stream() as any,
    stderr: stream() as any, patchConsole: false })

  try {
    await lifecycle.resumeById('linked')
    expect(getUiState().sid).toBe('linked')
    expect(request).toHaveBeenCalledWith('session.resume', expect.objectContaining({
      session_id: 'linked'
    }))
    // One attach round trip: the resume reply's own `linked_codex` marker decides, so the
    // transcript is read only AFTER the reply (no probe before it). The first page walk is
    // `loadInitial`, which fills the newest window under its byte budget.
    const methods = request.mock.calls.map(([method]) => method)
    expect(methods.indexOf('session.resume')).toBeLessThan(methods.indexOf('session.transcript'))
    expect(methods.filter(method => method === 'session.resume')).toHaveLength(1)
    expect(linkedMode).toHaveBeenLastCalledWith(true)
    expect(messages.some(message => message.text.startsWith('native-0'))).toBe(true)
    expect(messages.some(message => message.text.includes('STALE local mirror'))).toBe(false)
    expect(messages.some(message => message.text.startsWith('native-4'))).toBe(false)

    await lifecycle.loadLinkedHistory('older')
    expect(messages.some(message => message.text.startsWith('native-4'))).toBe(true)
    expect(messages.some(message => message.text.startsWith('native-0'))).toBe(false)
    await lifecycle.loadLinkedHistory('newer')
    expect(messages.some(message => message.text.startsWith('native-0'))).toBe(true)
    expect(sys).not.toHaveBeenCalled()
  } finally {
    instance.unmount()
    resetUiState()
    vi.unstubAllEnvs()
    rmSync(home, { recursive: true, force: true })
  }
})
